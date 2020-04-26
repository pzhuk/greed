import importlib
import os
import queue as queuem
import re
import sys
import threading
import traceback
import uuid
from datetime import datetime
from html import escape
from operator import attrgetter
from typing import Dict, List, Optional, Union

import requests
import telegram
from sqlalchemy import or_

import configloader
import database as db
import utils

import json
import logging

# set up logging to file - see previous section for more details
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
                    datefmt='%m-%d %H:%M',
                    filename='/tmp/bot.log',
                    filemode='w')

# define a Handler which writes INFO messages or higher to the sys.stderr
console = logging.StreamHandler()
console.setLevel(logging.DEBUG)
logging.getLogger('').addHandler(console)




language = configloader.config["Config"]["language"]
strings = importlib.import_module("strings." + language)

class StopSignal:
    """A data class that should be sent to the worker when the conversation has to be stopped abnormally."""

    def __init__(self, reason: str = ""):
        self.reason = reason


class CancelSignal:
    """An empty class that is added to the queue whenever the user presses a cancel inline button."""
    pass


class ChatWorker(threading.Thread):
    """A worker for a single conversation. A new one is created every time the /start command is sent."""

    def __init__(self, bot: utils.DuckBot, chat: telegram.Chat, *args, **kwargs):
        # Initialize the thread
        super().__init__(name=f"Worker {chat.id}", *args, **kwargs)
        # Store the bot and chat info inside the class
        self.bot: utils.DuckBot = bot
        self.chat: telegram.Chat = chat
        # Open a new database session
        self.session = db.Session()
        # Get the user db data from the users and admin tables
        self.user: Optional[db.User] = None
        self.admin: Optional[db.Admin] = None
        # The sending pipe is stored in the ChatWorker class, allowing the forwarding of messages to the chat process
        self.queue = queuem.Queue()
        # The current active invoice payload; reject all invoices with a different payload
        self.invoice_payload = None
        # The Sentry client for reporting errors encountered by the user
        if configloader.config["Error Reporting"]["sentry_token"] != \
                "https://00000000000000000000000000000000:00000000000000000000000000000000@sentry.io/0000000":
            import raven
            self.sentry_client = raven.Client(configloader.config["Error Reporting"]["sentry_token"],
                                              release=raven.fetch_git_sha(os.path.dirname(__file__)),
                                              environment="Dev" if __debug__ else "Prod")
        else:
            self.sentry_client = None

    # noinspection PyBroadException
    def run(self):
        """The conversation code."""
        # Welcome the user to the bot
        self.bot.send_message(self.chat.id, strings.conversation_after_start)
        # Get the user db data from the users and admin tables
        self.user = self.session.query(db.User).filter(db.User.user_id == self.chat.id).one_or_none()
        self.admin = self.session.query(db.Admin).filter(db.Admin.user_id == self.chat.id).one_or_none()
        # Create a dict to be used as 'cart'
        # The key is the message id of the product list
        # TODO: persist this in database, so user has continiuos shopping experience
        self.cart: Dict[List[db.Product, int]] = {}
        # If the user isn't registered, create a new record and add it to the db
        if self.user is None:
            # Check if there are other registered users: if there aren't any, the first user will be owner of the bot
            will_be_owner = (self.session.query(db.Admin).first() is None)
            # Create the new record
            self.user = db.User(self.chat)
            # Add the new record to the db
            self.session.add(self.user)
            # Flush the session to get an userid
            self.session.flush()
            # If the will be owner flag is set
            if will_be_owner:
                # Become owner
                self.admin = db.Admin(user_id=self.user.user_id,
                                      edit_products=True,
                                      receive_orders=True,
                                      create_transactions=True,
                                      display_on_help=True,
                                      is_owner=True,
                                      live_mode=False)
                # Add the admin to the transaction
                self.session.add(self.admin)
            # Commit the transaction
            self.session.commit()
        else:
            # Update users last seen date
            self.session.query(db.User).filter_by(user_id=self.user.user_id).update({'last_seen': datetime.now()})
            self.session.commit()
        # Capture exceptions that occour during the conversation
        try:
            # If the user is not an admin, send him to the user menu
            if self.admin is None:
                self.__order_menu()
            # If the user is an admin, send him to the admin menu
            else:
                # Clear the live orders flag
                self.admin.live_mode = False
                # Commit the change
                self.session.commit()
                # Open the admin menu
                self.__admin_menu()
        except Exception:
            # Try to notify the user of the exception
            try:
                self.bot.send_message(self.chat.id, strings.fatal_conversation_exception)
            except Exception:
                pass
            # If the Sentry integration is enabled, log the exception
            if self.sentry_client is not None:
                self.sentry_client.captureException()
            traceback.print_exception(*sys.exc_info())

    def stop(self, reason: str = ""):
        """Gracefully stop the worker process"""
        # Send a stop message to the thread
        self.queue.put(StopSignal(reason))
        # Wait for the thread to stop
        self.join()

    def update_user(self) -> db.User:
        """Update the user data.
        Note that this method will cause crashes if used in different threads with sqlite databases."""
        self.user = self.session.query(db.User).filter(db.User.user_id == self.chat.id).one_or_none()
        return self.user

    # noinspection PyUnboundLocalVariable
    def __receive_next_update(self) -> telegram.Update:
        """Get the next update from the queue.
        If no update is found, block the process until one is received.
        If a stop signal is sent, try to gracefully stop the thread."""
        # Pop data from the queue
        try:
            data = self.queue.get(timeout=int(configloader.config["Telegram"]["conversation_timeout"]))
        except queuem.Empty:
            # If the conversation times out, gracefully stop the thread
            self.__graceful_stop(StopSignal("timeout"))
        # Check if the data is a stop signal instance
        if isinstance(data, StopSignal):
            # Gracefully stop the process
            self.__graceful_stop(data)
        # Return the received update
        return data

    def __wait_for_specific_message(self,
                                    items: List[str],
                                    cancellable: bool = False) -> Union[str, CancelSignal]:
        """Continue getting updates until until one of the strings contained in the list is received as a message."""
        while True:
            # Get the next update
            update = self.__receive_next_update()
            # If a CancelSignal is received...
            if isinstance(update, CancelSignal):
                # And the wait is cancellable...
                if cancellable:
                    # Return the CancelSignal
                    return update
                else:
                    # Ignore the signal
                    continue
            # Ensure the update contains a message
            if update.message is None:
                continue
            # Ensure the message contains text
            if update.message.text is None:
                continue
            # Check if the message is contained in the list
            if update.message.text not in items:
                continue
            # Return the message text
            return update.message.text

    def __wait_for_regex(self, regex: str, cancellable: bool = False) -> Union[str, CancelSignal]:
        """Continue getting updates until the regex finds a match in a message, then return the first capture group."""
        while True:
            # Get the next update
            update = self.__receive_next_update()
            # If a CancelSignal is received...
            if isinstance(update, CancelSignal):
                # And the wait is cancellable...
                if cancellable:
                    # Return the CancelSignal
                    return update
                else:
                    # Ignore the signal
                    continue
            # Ensure the update contains a message
            if update.message is None:
                continue
            # Ensure the message contains text
            if update.message.text is None:
                continue
            # Try to match the regex with the received message
            match = re.search(regex, update.message.text)
            # Ensure there is a match
            if match is None:
                continue
            # Return the first capture group
            return match.group(1) or match.group(2)

    def __wait_for_precheckoutquery(self,
                                    cancellable: bool = False) -> Union[telegram.PreCheckoutQuery, CancelSignal]:
        """Continue getting updates until a precheckoutquery is received.
        The payload is checked by the core before forwarding the message."""
        while True:
            # Get the next update
            update = self.__receive_next_update()
            # If a CancelSignal is received...
            if isinstance(update, CancelSignal):
                # And the wait is cancellable...
                if cancellable:
                    # Return the CancelSignal
                    return update
                else:
                    # Ignore the signal
                    continue
            # Ensure the update contains a precheckoutquery
            if update.pre_checkout_query is None:
                continue
            # Return the precheckoutquery
            return update.pre_checkout_query

    def __wait_for_successfulpayment(self) -> telegram.SuccessfulPayment:
        """Continue getting updates until a successfulpayment is received."""
        while True:
            # Get the next update
            update = self.__receive_next_update()
            # Ensure the update contains a message
            if update.message is None:
                continue
            # Ensure the message is a successfulpayment
            if update.message.successful_payment is None:
                continue
            # Return the successfulpayment
            return update.message.successful_payment

    def __wait_for_photo(self, cancellable: bool = False) -> Union[List[telegram.PhotoSize], CancelSignal]:
        """Continue getting updates until a photo is received, then return it."""
        while True:
            # Get the next update
            update = self.__receive_next_update()
            # If a CancelSignal is received...
            if isinstance(update, CancelSignal):
                # And the wait is cancellable...
                if cancellable:
                    # Return the CancelSignal
                    return update
                else:
                    # Ignore the signal
                    continue
            # Ensure the update contains a message
            if update.message is None:
                continue
            # Ensure the message contains a photo
            if update.message.photo is None:
                continue
            # If photo message has been sent
            if len(update.message.photo) > 0:
                # Find object with maximum width
                photo = max(update.message.photo, key=attrgetter('width'))
                break
            else:
                self.bot.send_message(self.chat.id, strings.downloading_image_failed)
                continue

        # If a photo has been sent...
        # Notify the user that the bot is downloading the image and might be inactive for a while
        self.bot.send_message(self.chat.id, strings.downloading_image)
        self.bot.send_chat_action(self.chat.id, action="upload_photo")
        # Return file object associated with the photo
        return photo.file_id

    def __wait_for_inlinekeyboard_callback(self, cancellable: bool = False) \
            -> Union[telegram.CallbackQuery, CancelSignal]:
        """Continue getting updates until an inline keyboard callback is received, then return it."""
        while True:
            # Get the next update
            update = self.__receive_next_update()
            # If a CancelSignal is received...
            if isinstance(update, CancelSignal):
                # And the wait is cancellable...
                if cancellable:
                    # Return the CancelSignal
                    return update
                else:
                    # Ignore the signal
                    continue
            # Ensure the update is a CallbackQuery
            if update.callback_query is None:
                continue
            # Answer the callbackquery
            self.bot.answer_callback_query(update.callback_query.id)
            # Return the callbackquery
            return update.callback_query

    def __user_select(self) -> Union[db.User, CancelSignal]:
        """Select an user from the ones in the database."""
        # Find first ten users in the database
        users = self.session.query(db.User) \
            .order_by(db.User.last_seen.desc()) \
            .limit(5) \
            .all()
        # Create a list containing all the keyboard button strings
        keyboard_buttons = [[strings.menu_cancel]]
        # Add to the list all the users
        for user in users:
            keyboard_buttons.append([user.identifiable_str()])
        # Create the keyboard
        keyboard = telegram.ReplyKeyboardMarkup(keyboard_buttons, one_time_keyboard=True)
        # Keep asking until a result is returned
        while True:
            # Send the keyboard
            self.bot.send_message(self.chat.id, strings.conversation_admin_select_user, reply_markup=keyboard)
            # Wait for a reply
            reply = self.__wait_for_regex("user_([0-9]+)|\@(.*)", cancellable=True)
            # Propagate CancelSignals
            if isinstance(reply, CancelSignal):
                return reply
            # Find the user in the database
            user = self.session.query(db.User) \
                .filter(or_(db.User.user_id==reply, db.User.username==reply)) \
                .one_or_none()
            # Ensure the user exists
            if not user:
                self.bot.send_message(self.chat.id, strings.error_user_does_not_exist)
                continue
            return user

    def __user_menu(self):
        """Function called from the run method when the user is not an administrator.
        Normal bot actions should be placed here."""
        # Loop used to returning to the menu after executing a command
        while True:
            # Create a keyboard with the user main menu
            keyboard = [[telegram.KeyboardButton(strings.menu_order)],
                        [telegram.KeyboardButton(strings.menu_order_status)],
                        [telegram.KeyboardButton(strings.menu_add_credit)],
                        [telegram.KeyboardButton(strings.menu_help)]]
            # Send the previously created keyboard to the user (ensuring it can be clicked only 1 time)
            self.bot.send_message(self.chat.id,
                                  strings.conversation_open_user_menu.format(credit=utils.Price(self.user.credit)),
                                  reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
            # Wait for a reply from the user
            selection = self.__wait_for_specific_message([strings.menu_order, strings.menu_order_status,
                                                          strings.menu_add_credit, strings.menu_bot_info,
                                                          strings.menu_help])
            # After the user reply, update the user data
            self.update_user()
            # If the user has selected the Order option...
            if selection == strings.menu_order:
                # Open the order menu
                self.__order_menu()
            # If the user has selected the Order Status option...
            elif selection == strings.menu_order_status:
                # Display the order(s) status
                self.__order_status()
            # If the user has selected the Add Credit option...
            elif selection == strings.menu_add_credit:
                # Display the add credit menu
                self.__add_credit_menu()
            # If the user has selected the Bot Info option...
            elif selection == strings.menu_bot_info:
                # Display information about the bot
                self.__bot_info()
            # If the user has selected the Help option...
            elif selection == strings.menu_help:
                # Go to the Help menu
                self.__help_menu()

    def __display_product_item(self, product, message=None):
        inline_keyboard = \
            [[telegram.InlineKeyboardButton(strings.menu_add_to_cart,callback_data="cart_add")]]

        # Display product item for the first time
        if (not message):
            if (not product.image):
                message = self.bot.send_message(
                    self.chat.id,
                    product.text(),
                    reply_markup=telegram.InlineKeyboardMarkup(inline_keyboard))
            else:
                message = self.bot.send_photo(
                    self.chat.id,
                    product.image,
                    caption=product.text(),
                    parse_mode="HTML",
                    reply_markup=telegram.InlineKeyboardMarkup(inline_keyboard))
            self.cart[message.message_id] = [product, 0]
            return message
        # Update product item in the correspondent message after cart add/remove action
        # Get quantity or product or set to None
        qty = self.cart[message.message_id][1]
        # Show remove putton in the inline keyboard if cart already has at least one item of a product
        if qty > 0:
            inline_keyboard.append([telegram.InlineKeyboardButton(strings.menu_remove_from_cart,callback_data="cart_remove")])

        if product.image:
            self.bot.edit_message_caption(chat_id=self.chat.id,
                                            message_id=message.message_id,
                                            caption=product.text(cart_qty=qty or None),
                                            reply_markup=telegram.InlineKeyboardMarkup(inline_keyboard))
        else:
            self.bot.edit_message_text(chat_id=self.chat.id,
                            message_id=message.message_id,
                            text=product.text(cart_qty=qty or None),
                            reply_markup=telegram.InlineKeyboardMarkup(inline_keyboard))
        return message

    def __display_cart_final_msg(self, message=None):
        # Create default keyboard with the cancel button
        keyboard = [[telegram.InlineKeyboardButton(strings.menu_cancel,callback_data="cart_cancel")], \
                    [telegram.InlineKeyboardButton(strings.menu_category_change,callback_data="category_change")]]  #TODO: update translations
        # Generate default text with cart actions
        text = strings.conversation_cart_actions
        # If cart has already any products- display summary
        if self.__get_cart_value() > 0:
            text = strings.conversation_confirm_cart.format(product_list=self.__get_cart_summary(),
                                                            total_cost=str(self.__get_cart_value()))
            # Also ensure to display done button in final message
            keyboard.append([telegram.InlineKeyboardButton(strings.menu_done, callback_data="cart_done")])

        markup = telegram.InlineKeyboardMarkup(keyboard)
        # Send a message containing the button to cancel or pay
        if not message:
            message = self.bot.send_message(self.chat.id, text, reply_markup=markup)
        else:
            self.bot.edit_message_text(
                chat_id=self.chat.id,
                message_id=message.message_id,
                text=text,
                reply_markup=markup)
        return message

    def __display_products_from_category(self, category=None):
        # Display from default category if other argument wasn't given
        if not category:
            category = self.session.query(db.ProductCategory).filter_by(default=True, deleted=False).one()

        # Initialize the products list
        for product in category.products:
            # If the product is not for sale, don't display it
            if product.price is None or product.deleted:
                continue
            # Display product
            self.__display_product_item(product=product)

    def __select_product_category(self):
        categories = self.session.query(db.ProductCategory).filter_by(deleted=False).all()
        # Add to the list all the categories

        options = [category.name for category in categories]
        keyboard_buttons = [[telegram.KeyboardButton(option)] for option in options]

        keyboard_buttons.append([telegram.KeyboardButton(strings.menu_order_status), telegram.KeyboardButton(strings.menu_contact_shopkeeper)])
        options.append(strings.menu_order_status)
        options.append(strings.menu_contact_shopkeeper)


        # Create the keyboard
        keyboard = telegram.ReplyKeyboardMarkup(keyboard_buttons, one_time_keyboard=True)

        while True:
            self.bot.send_message(self.chat.id, strings.select_category_name, reply_markup=keyboard)
            response = self.__wait_for_specific_message(options, cancellable=True)

            # Return if canceled
            if isinstance(response, CancelSignal):
                return response

            if response == strings.menu_order_status:
                self.__order_status()
                continue

            if response == strings.menu_contact_shopkeeper:
                self.bot.send_message(self.chat.id, strings.contact_shopkeeper)
                continue

            category = self.session.query(db.ProductCategory) \
                .filter_by(name=str(response)) \
                .one_or_none()

            if category:
                return category

    def __order_menu(self):
        """User menu to order products from the shop."""
        # Ask to select category
        category = self.__select_product_category()

        self.__display_products_from_category(category)
        # Display final summary message
        final_msg = self.__display_cart_final_msg()
        # Wait for user input
        while True:
            callback = self.__wait_for_inlinekeyboard_callback()
            # React to the user input
            # If the cancel button has been pressed...
            if callback.data == "cart_cancel":
                # Stop waiting for user input and go back to the previous menu
                return
            # If a Add to Cart button has been pressed...
            elif callback.data == "cart_add":
                # Get the selected product, ensuring it exists
                p = self.cart.get(callback.message.message_id)
                if p is None:
                    continue
                product = p[0]
                # Add 1 copy to the cart
                self.cart[callback.message.message_id][1] += 1
                # Edit both the product and the final message
                self.__display_product_item(product=product, message=callback.message)
                self.__display_cart_final_msg(message=final_msg)
            # If the Remove from cart button has been pressed...
            elif callback.data == "cart_remove":
                # Get the selected product, ensuring it exists
                p = self.cart.get(callback.message.message_id)
                if p is None:
                    continue
                product = p[0]
                # Remove 1 copy from the cart
                if self.cart[callback.message.message_id][1] > 0:
                    self.cart[callback.message.message_id][1] -= 1
                else:
                    continue
                # Edit both the product and the final message
                self.__display_product_item(product=product, message=callback.message)
                self.__display_cart_final_msg(message=final_msg)
            # If the done button has been pressed...
            elif callback.data == "cart_done":
                # End the loop
                break
            elif callback.data == "category_change":
                response = self.__select_product_category()
                if isinstance(response, db.ProductCategory):
                    self.__display_products_from_category(response)
                    final_msg = self.__display_cart_final_msg()
                elif isinstance(response, CancelSignal):
                    self.__display_products_from_category()
                    final_msg = self.__display_cart_final_msg()

        # Ask if the user wants to add notes to the order
        self.bot.send_message(self.chat.id, strings.ask_order_notes)
        # Wait for user input
        notes = self.__wait_for_regex(r"(.*)", cancellable=True)
        # Create a new Order
        order = db.Order(user=self.user,
                         creation_date=datetime.now(),
                         notes=notes if not isinstance(notes, CancelSignal) else "")
        # Add the record to the session and get an ID
        self.session.add(order)
        self.session.flush()
        # For each product added to the cart, create a new OrderItem
        for product in self.cart:
            # Create {quantity} new OrderItems
            for i in range(0, self.cart[product][1]):
                order_item = db.OrderItem(product=self.cart[product][0],
                                          order_id=order.order_id)
                self.session.add(order_item)
        # Ensure the user has enough credit to make the purchase
        credit_required = self.__get_cart_value() - self.user.credit
        # Notify user in case of insufficient credit
        if credit_required > 0:
            self.bot.send_message(self.chat.id, strings.error_not_enough_credit)
            # Suggest payment for missing credit value if configuration allows refill
            if configloader.config["Credit Card"]["credit_card_token"] != "" \
                    and configloader.config["Appearance"]["refill_on_checkout"] == 'yes' \
                    and credit_required <= utils.Price(int(configloader.config["Credit Card"]["max_amount"])) \
                    and credit_required >= utils.Price(int(configloader.config["Credit Card"]["min_amount"])):
                self.__make_payment(utils.Price(credit_required))
        # If afer requested payment credit is still insufficient (either payment failure or cancel)
        if self.user.credit < self.__get_cart_value():
            # Rollback all the changes
            self.session.rollback()
        else:
            # User has credit and valid order, perform transaction now
            self.__order_transaction(order=order, value=-int(self.__get_cart_value()))

    def __get_cart_value(self):
        # Calculate total items value in cart
        value = utils.Price(0)
        for product in self.cart:
            value += self.cart[product][0].price * self.cart[product][1]
        return value

    def __get_cart_summary(self):
        # Create the cart summary
        product_list = ""
        for product_id in self.cart:
            if self.cart[product_id][1] > 0:
                product_list += self.cart[product_id][0].text(style="short", cart_qty=self.cart[product_id][1]) + "\n"
        return product_list

    def __order_transaction(self, order, value):
        # Create a new transaction and add it to the session
        transaction = db.Transaction(user=self.user,
                                     value=value,
                                     order_id=order.order_id)
        self.session.add(transaction)
        # Commit all the changes
        self.session.commit()
        # Update the user's credit
        self.user.recalculate_credit()
        # Commit all the changes
        self.session.commit()
        # Notify admins about new transation
        self.__order_notify_admins(order=order)

    def __order_notify_admins(self, order):
        # Notify the user of the order result
        self.bot.send_message(self.chat.id, strings.success_order_created.format(order=order.get_text(self.session,
                                                                                                      user=True)))
        # Notify the admins (in Live Orders mode) of the new order
        admins = self.session.query(db.Admin).filter_by(live_mode=True).all()
        # Create the order keyboard
        order_keyboard = telegram.InlineKeyboardMarkup(
            [
                [telegram.InlineKeyboardButton(strings.menu_complete, callback_data="order_complete")],
                [telegram.InlineKeyboardButton(strings.menu_refund, callback_data="order_refund")]
            ])
        # Notify them of the new placed order
        for admin in admins:
            self.bot.send_message(admin.user_id,
                                  f"{strings.notification_order_placed.format(order=order.get_text(self.session))}",
                                  reply_markup=order_keyboard)

    def __order_status(self):
        """Display the status of the sent orders."""
        # Find the latest orders
        orders = self.session.query(db.Order) \
            .filter(db.Order.user == self.user) \
            .order_by(db.Order.creation_date.desc()) \
            .limit(20) \
            .all()
        # Ensure there is at least one order to display
        if len(orders) == 0:
            self.bot.send_message(self.chat.id, strings.error_no_orders)
        # Display the order status to the user
        for order in orders:
            self.bot.send_message(self.chat.id, order.get_text(self.session, user=True))
        # TODO: maybe add a page displayer instead of showing the latest 5 orders

    def __add_credit_menu(self):
        """Add more credit to the account."""
        # Create a payment methods keyboard
        keyboard = list()
        # Add the supported payment methods to the keyboard
        # Cash
        keyboard.append([telegram.KeyboardButton(strings.menu_cash)])
        # Telegram Payments
        if configloader.config["Credit Card"]["credit_card_token"] != "":
            keyboard.append([telegram.KeyboardButton(strings.menu_credit_card)])
        # Keyboard: go back to the previous menu
        keyboard.append([telegram.KeyboardButton(strings.menu_cancel)])
        # Send the keyboard to the user
        self.bot.send_message(self.chat.id, strings.conversation_payment_method,
                              reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
        # Wait for a reply from the user
        selection = self.__wait_for_specific_message([strings.menu_cash, strings.menu_credit_card, strings.menu_cancel],
                                                     cancellable=True)
        # If the user has selected the Cash option...
        if selection == strings.menu_cash:
            # Go to the pay with cash function
            self.bot.send_message(self.chat.id,
                                  strings.payment_cash.format(user_cash_id=self.user.identifiable_str()))
        # If the user has selected the Credit Card option...
        elif selection == strings.menu_credit_card:
            # Go to the pay with credit card function
            self.__add_credit_cc()
        # If the user has selected the Cancel option...
        elif isinstance(selection, CancelSignal):
            # Send him back to the previous menu
            return

    def __add_credit_cc(self):
        """Add money to the wallet through a credit card payment."""
        # Create a keyboard to be sent later
        presets = list(map(lambda s: s.strip(" "), configloader.config["Credit Card"]["payment_presets"].split('|')))
        keyboard = [[telegram.KeyboardButton(str(utils.Price(preset)))] for preset in presets]
        keyboard.append([telegram.KeyboardButton(strings.menu_cancel)])
        # Boolean variable to check if the user has cancelled the action
        cancelled = False
        # Loop used to continue asking if there's an error during the input
        while not cancelled:
            # Send the message and the keyboard
            self.bot.send_message(self.chat.id, strings.payment_cc_amount,
                                  reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
            # Wait until a valid amount is sent
            selection = self.__wait_for_regex(r"([0-9]+(?:[.,][0-9]+)?|" + strings.menu_cancel + r")", cancellable=True)
            # If the user cancelled the action
            if isinstance(selection, CancelSignal):
                # Exit the loop
                cancelled = True
                continue
            # Convert the amount to an integer
            value = utils.Price(selection)
            # Ensure the amount is within the range
            if value > utils.Price(int(configloader.config["Credit Card"]["max_amount"])):
                self.bot.send_message(self.chat.id,
                                      strings.error_payment_amount_over_max.format(
                                          max_amount=utils.Price(configloader.config["Credit Card"]["max_amount"]))
                                      )
                continue
            elif value < utils.Price(int(configloader.config["Credit Card"]["min_amount"])):
                self.bot.send_message(self.chat.id,
                                      strings.error_payment_amount_under_min.format(
                                          min_amount=utils.Price(configloader.config["Credit Card"]["min_amount"]))
                                      )
                continue
            break
        # If the user cancelled the action...
        else:
            # Exit the function
            return
        # Issue the payment invoice
        self.__make_payment(amount=value)

    def __make_payment(self, amount):
        # Set the invoice active invoice payload
        self.invoice_payload = str(uuid.uuid4())
        # Create the price array
        prices = [telegram.LabeledPrice(label=strings.payment_invoice_label, amount=int(amount))]
        # If the user has to pay a fee when using the credit card, add it to the prices list
        fee = int(self.__get_total_fee(amount))
        if fee > 0:
            prices.append(telegram.LabeledPrice(label=strings.payment_invoice_fee_label,
                                                amount=fee))
        # Create the invoice keyboard
        inline_keyboard = telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(strings.menu_pay, pay=True)],
                                                         [telegram.InlineKeyboardButton(strings.menu_cancel,
                                                                                        callback_data="cmd_cancel")]])
        # The amount is valid, send the invoice
        self.bot.send_invoice(self.chat.id,
                              title=strings.payment_invoice_title,
                              description=strings.payment_invoice_description.format(amount=str(amount)),
                              payload=self.invoice_payload,
                              provider_token=configloader.config["Credit Card"]["credit_card_token"],
                              start_parameter="tempdeeplink",
                              currency=configloader.config["Payments"]["currency"],
                              prices=prices,
                              need_name=configloader.config["Credit Card"]["name_required"] == "yes",
#                              need_email=configloader.config["Credit Card"]["email_required"] == "yes",
                              need_phone_number=configloader.config["Credit Card"]["phone_required"] == "yes",
#                              need_shipping_address=True,
                              #reply_markup=inline_keyboard
                              )
        # Wait for the precheckout query
        precheckoutquery = self.__wait_for_precheckoutquery(cancellable=True)
        # Check if the user has cancelled the invoice
        if isinstance(precheckoutquery, CancelSignal):
            # Exit the function
            return
        # Accept the checkout
        self.bot.answer_pre_checkout_query(precheckoutquery.id, ok=True)
        # Wait for the payment
        successfulpayment = self.__wait_for_successfulpayment()
        # Create a new database transaction
        transaction = db.Transaction(user=self.user,
                                     value=int(successfulpayment.total_amount) - fee,
                                     provider="Credit Card",
                                     telegram_charge_id=successfulpayment.telegram_payment_charge_id,
                                     provider_charge_id=successfulpayment.provider_payment_charge_id)

        if successfulpayment.order_info is not None:
            transaction.payment_name = successfulpayment.order_info.name
            transaction.payment_email = successfulpayment.order_info.email
            transaction.payment_phone = successfulpayment.order_info.phone_number
        # Update the user's credit
        self.user.recalculate_credit()
        # Commit all the changes
        self.session.commit()

    @staticmethod
    def __get_total_fee(amount):
        # Calculate a fee for the required amount
        fee_percentage = float(configloader.config["Credit Card"]["fee_percentage"]) / 100
        fee_fixed = int(configloader.config["Credit Card"]["fee_fixed"])
        total_fee = amount * fee_percentage + fee_fixed
        if total_fee > 0:
            return total_fee
        # Set the fee to 0 to ensure no accidental discounts are applied
        return 0

    def __bot_info(self):
        """Send information about the bot."""
        self.bot.send_message(self.chat.id, strings.bot_info)

    def __admin_menu(self):
        """Function called from the run method when the user is an administrator.
        Administrative bot actions should be placed here."""
        # Loop used to return to the menu after executing a command
        while True:
            # Create a keyboard with the admin main menu based on the admin permissions specified in the db
            keyboard = []
            if self.admin.edit_products:
                keyboard.append([strings.menu_products, strings.menu_categories])
            if self.admin.receive_orders:
                keyboard.append([strings.menu_orders])
            if self.admin.create_transactions:
                keyboard.append([strings.menu_edit_credit])
                keyboard.append([strings.menu_transactions, strings.menu_csv])
            if self.admin.is_owner:
                keyboard.append([strings.menu_edit_admins])
            keyboard.append([strings.menu_user_mode])
            # Send the previously created keyboard to the user (ensuring it can be clicked only 1 time)
            self.bot.send_message(self.chat.id, strings.conversation_open_admin_menu,
                                  reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True),
                                  )
            # Wait for a reply from the user
            selection = self.__wait_for_specific_message([strings.menu_products, strings.menu_orders, strings.menu_categories,
                                                          strings.menu_user_mode, strings.menu_edit_credit,
                                                          strings.menu_transactions, strings.menu_csv,
                                                          strings.menu_edit_admins])
            # If the user has selected the Products option...
            if selection == strings.menu_products:
                # Open the products menu
                self.__products_menu()
            elif selection == strings.menu_categories:
                self.__categories_menu()
            # If the user has selected the Orders option...
            elif selection == strings.menu_orders:
                # Open the orders menu
                self.__orders_menu()
            # If the user has selected the Transactions option...
            elif selection == strings.menu_edit_credit:
                # Open the edit credit menu
                self.__create_transaction()
            # If the user has selected the User mode option...
            elif selection == strings.menu_user_mode:
                # Tell the user how to go back to admin menu
                self.bot.send_message(self.chat.id, strings.conversation_switch_to_user_mode)
                # Start the bot in user mode
                self.__order_menu()
            # If the user has selected the Add Admin option...
            elif selection == strings.menu_edit_admins:
                # Open the edit admin menu
                self.__add_admin()
            # If the user has selected the Transactions option...
            elif selection == strings.menu_transactions:
                # Open the transaction pages
                self.__transaction_pages()
            # If the user has selected the .csv option...
            elif selection == strings.menu_csv:
                # Generate the .csv file
                self.__transactions_file()

    def __categories_menu(self):
        category = self.__create_product_category()
        if not isinstance(category, db.ProductCategory):
            return

        options = []
        options.insert(0, strings.menu_cancel)
        options.insert(1, strings.menu_edit_category)
        options.insert(2, strings.menu_remove_category)
        options.insert(3, strings.menu_make_category_default)
        keyboard = [[telegram.KeyboardButton(option)] for option in options]
        self.bot.send_message(self.chat.id, strings.conversation_admin_select_category,
                              reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
        selection = self.__wait_for_specific_message(options, cancellable=True)
        # If the user has selected the Cancel option...
        if isinstance(selection, CancelSignal):
            # Exit the menu
            return
        elif selection == strings.menu_edit_category:
            self.bot.send_message(self.chat.id, strings.menu_rename_category)
            name = self.__wait_for_regex(r"(.*)", cancellable=False)
            category.name=name
            self.session.commit()
            self.bot.send_message(self.chat.id, strings.conversation_rename_category_succeed)
        elif selection == strings.menu_remove_category:
            products = self.session.query(db.Product) \
                .filter_by(deleted=False, category_id=category.id) \
                .count()
            if products > 0:
                self.bot.send_message(self.chat.id, strings.conversation_delete_category_products)
            else:
                if category.default:
                    self.bot.send_message(self.chat.id, strings.conversation_reassign_default_category)
                    newdc = self.__create_product_category()
                    self.bot.send_message(self.chat.id, strings.conversation_new_assigned_category.format(name=newdc.name))
                    newdc.default = True
                self.bot.send_message(self.chat.id, strings.conversation_remove_category_succeed.format(name=category.name))
                self.session.delete(category)
                self.session.commit()
        elif selection == strings.menu_make_category_default:
            dc = self.session.query(db.ProductCategory) \
                .filter_by(deleted=False, default=True) \
                .one()
            dc.default = False
            category.default = True
            self.session.commit()
            self.bot.send_message(self.chat.id, strings.conversation_default_category_succeed)

    def __products_menu(self):
        """Display the admin menu to select a product to edit."""
        # Get the products list from the db
        products = self.session.query(db.Product).filter_by(deleted=False).all()
        # Create a list of product names
        product_names = [product.name for product in products]
        # Insert at the start of the list the add product option, the remove product option and the Cancel option
        product_names.insert(0, strings.menu_cancel)
        product_names.insert(1, strings.menu_add_product)
        product_names.insert(2, strings.menu_delete_product)
        # Create a keyboard using the product names
        keyboard = [[telegram.KeyboardButton(product_name)] for product_name in product_names]
        # Send the previously created keyboard to the user (ensuring it can be clicked only 1 time)
        self.bot.send_message(self.chat.id, strings.conversation_admin_select_product,
                              reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
        # Wait for a reply from the user
        selection = self.__wait_for_specific_message(product_names, cancellable=True)
        # If the user has selected the Cancel option...
        if isinstance(selection, CancelSignal):
            # Exit the menu
            return
        # If the user has selected the Add Product option...
        elif selection == strings.menu_add_product:
            # Open the add product menu
            self.__edit_product_menu()
        # If the user has selected the Remove Product option...
        elif selection == strings.menu_delete_product:
            # Open the delete product menu
            self.__delete_product_menu()
        # If the user has selected a product
        else:
            # Find the selected product
            product = self.session.query(db.Product).filter_by(name=selection, deleted=False).one()
            # Open the edit menu for that specific product
            self.__edit_product_menu(product=product)

    def __create_product_category(self) -> Union[db.ProductCategory, CancelSignal]:
        """Add a product category or create a new one"""
        response = self.__select_product_category()
        # Cancel was taken
        if isinstance(response, CancelSignal):
            return response
        # Existing category was selected
        if isinstance(response, db.ProductCategory):
            return response
        # Create category if id wasn't matched (manually entered name)
        else:
            named_categories = bool(self.session.query(db.ProductCategory) \
                .filter_by(name=response, deleted=False) \
                .count())
            if named_categories:
                self.bot.send_message(self.chat.id, strings.conversation_admin_category_exist)
                return

            categories = bool(self.session.query(db.ProductCategory) \
                .filter_by(deleted=False)
                .count())

            category = db.ProductCategory(name=response, default=(not bool(categories)))
            self.session.add(category)
            self.session.commit()
        return category

    def __edit_product_menu(self, product: Optional[db.Product] = None):
        """Add a product to the database or edit an existing one."""
        # Create an inline keyboard with a single skip button
        cancel = telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(strings.menu_skip,
                                                                               callback_data="cmd_cancel")]])
        # Ask for the product name until a valid product name is specified
        while True:
            # Ask the question to the user
            self.bot.send_message(self.chat.id, strings.ask_product_name)
            # Display the current name if you're editing an existing product
            if product:
                self.bot.send_message(self.chat.id, strings.edit_current_value.format(value=escape(product.name)),
                                      reply_markup=cancel)
            # Wait for an answer
            name = self.__wait_for_regex(r"(.*)", cancellable=bool(product))
            # Ensure a product with that name doesn't already exist
            if (product and isinstance(name, CancelSignal)) or \
                    self.session.query(db.Product).filter_by(name=name, deleted=False).one_or_none() in [None, product]:
                # Exit the loop
                break
            self.bot.send_message(self.chat.id, strings.error_duplicate_name)
        # Select a category for the product
        category = self.__create_product_category()
        # Ask for the product description
        self.bot.send_message(self.chat.id, strings.ask_product_description)
        # Display the current description if you're editing an existing product
        if product:
            self.bot.send_message(self.chat.id,
                                  strings.edit_current_value.format(value=escape(product.description)),
                                  reply_markup=cancel)
        # Wait for an answer
        description = self.__wait_for_regex(r"(.*)", cancellable=bool(product))
        # Ask for the product price
        self.bot.send_message(self.chat.id,
                              strings.ask_product_price)
        # Display the current name if you're editing an existing product
        if product:
            self.bot.send_message(self.chat.id,
                                  strings.edit_current_value.format(
                                      value=(str(utils.Price(product.price))
                                             if product.price is not None else 'Non in vendita')),
                                  reply_markup=cancel)
        # Wait for an answer
        price = self.__wait_for_regex(r"([0-9]+(?:[.,][0-9]{1,2})?|[Xx])",
                                      cancellable=True)
        # If the price is skipped
        if isinstance(price, CancelSignal):
            pass
        elif price.lower() == "x":
            price = None
        else:
            price = utils.Price(price)

        # Ask for the product image
        self.bot.send_message(self.chat.id, strings.ask_product_image, reply_markup=cancel)
        # Wait for an answer
        image = self.__wait_for_photo(cancellable=True)
        # If a new product is being added...
        if not product:
            # Create the db record for the product
            # noinspection PyTypeChecker
            product = db.Product(name=name,
                                 description=description,
                                 category_id=category.id,
                                 price=int(price) or None,
                                 image=image if not isinstance(image, CancelSignal) else None,
                                 deleted=False)
            # Add the record to the database
            self.session.add(product)
            self.session.flush()
        # If a product is being edited...
        else:
            # Edit the record with the new values
            product.name = name if not isinstance(name, CancelSignal) else product.name
            product.description = description if not isinstance(description, CancelSignal) else product.description
            product.price = int(price) if not isinstance(price, CancelSignal) else product.price
            product.image = image if not isinstance(image, CancelSignal) else None,
        # Commit the session changes
        self.session.commit()
        # Notify the user
        self.bot.send_message(self.chat.id, strings.success_product_edited)

    def __delete_product_menu(self):
        # Get the products list from the db
        products = self.session.query(db.Product).filter_by(deleted=False).all()
        # Create a list of product names
        product_names = [product.name for product in products]
        # Insert at the start of the list the Cancel button
        product_names.insert(0, strings.menu_cancel)
        # Create a keyboard using the product names
        keyboard = [[telegram.KeyboardButton(product_name)] for product_name in product_names]
        # Send the previously created keyboard to the user (ensuring it can be clicked only 1 time)
        self.bot.send_message(self.chat.id, strings.conversation_admin_select_product_to_delete,
                              reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
        # Wait for a reply from the user
        selection = self.__wait_for_specific_message(product_names, cancellable=True)
        if isinstance(selection, CancelSignal):
            # Exit the menu
            return
        else:
            # Find the selected product
            product = self.session.query(db.Product).filter_by(name=selection, deleted=False).one()
            # "Delete" the product by setting the deleted flag to true
            product.deleted = True
            self.session.commit()
            # Notify the user
            self.bot.send_message(self.chat.id, strings.success_product_deleted)

    def __orders_menu(self):
        """Display a live flow of orders."""
        # Create a cancel and a stop keyboard
        stop_keyboard = telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(strings.menu_stop,
                                                                                      callback_data="cmd_cancel")]])
        cancel_keyboard = telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(strings.menu_cancel,
                                                                                        callback_data="cmd_cancel")]])
        # Send a small intro message on the Live Orders mode
        self.bot.send_message(self.chat.id, strings.conversation_live_orders_start, reply_markup=stop_keyboard)
        # Create the order keyboard
        order_keyboard = telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(strings.menu_complete,
                                                                                       callback_data="order_complete")],
                                                        [telegram.InlineKeyboardButton(strings.menu_refund,
                                                                                       callback_data="order_refund")]])
        # Display the past pending orders
        orders = self.session.query(db.Order) \
            .filter_by(delivery_date=None, refund_date=None) \
            .join(db.Transaction) \
            .join(db.User) \
            .all()
        # Create a message for every one of them
        for order in orders:
            # Send the created message
            self.bot.send_message(self.chat.id, order.get_text(session=self.session),
                                  reply_markup=order_keyboard)
        # Set the Live mode flag to True
        self.admin.live_mode = True
        # Commit the change to the database
        self.session.commit()
        while True:
            # Wait for any message to stop the listening mode
            update = self.__wait_for_inlinekeyboard_callback(cancellable=True)
            # If the user pressed the stop button, exit listening mode
            if isinstance(update, CancelSignal):
                # Stop the listening mode
                self.admin.live_mode = False
                break
            # Find the order
            order_id = re.search(strings.order_number.replace("{id}", "([0-9]+)"), update.message.text).group(1)
            order = self.session.query(db.Order).filter(db.Order.order_id == order_id).one()
            # Check if the order hasn't been already cleared
            if order.delivery_date is not None or order.refund_date is not None:
                # Notify the admin and skip that order
                self.bot.edit_message_text(self.chat.id, strings.error_order_already_cleared)
                break
            # If the user pressed the complete order button, complete the order
            if update.data == "order_complete":
                # Mark the order as complete
                order.delivery_date = datetime.now()
                # Commit the transaction
                self.session.commit()
                # Update order message
                self.bot.edit_message_text(order.get_text(session=self.session), chat_id=self.chat.id,
                                           message_id=update.message.message_id)
                # Notify the user of the completition
                self.bot.send_message(order.user_id,
                                      strings.notification_order_completed.format(order=order.get_text(self.session,
                                                                                                       user=True)))
            # If the user pressed the refund order button, refund the order...
            elif update.data == "order_refund":
                # Ask for a refund reason
                reason_msg = self.bot.send_message(self.chat.id, strings.ask_refund_reason,
                                                   reply_markup=cancel_keyboard)
                # Wait for a reply
                reply = self.__wait_for_regex("(.*)", cancellable=True)
                # If the user pressed the cancel button, cancel the refund
                if isinstance(reply, CancelSignal):
                    # Delete the message asking for the refund reason
                    self.bot.delete_message(self.chat.id, reason_msg.message_id)
                    continue
                # Mark the order as refunded
                order.refund_date = datetime.now()
                # Save the refund reason
                order.refund_reason = reply
                # Refund the credit, reverting the old transaction
                order.transaction.refunded = True
                # Update the user's credit
                order.user.recalculate_credit()
                # Commit the changes
                self.session.commit()
                # Update the order message
                self.bot.edit_message_text(order.get_text(session=self.session),
                                           chat_id=self.chat.id,
                                           message_id=update.message.message_id)
                # Notify the user of the refund
                self.bot.send_message(order.user_id,
                                      strings.notification_order_refunded.format(order=order.get_text(self.session,
                                                                                                      user=True)))
                # Notify the admin of the refund
                self.bot.send_message(self.chat.id, strings.success_order_refunded.format(order_id=order.order_id))

    def __create_transaction(self):
        """Edit manually the credit of an user."""
        # Make the admin select an user
        user = self.__user_select()
        # Allow the cancellation of the operation
        if isinstance(user, CancelSignal):
            return
        # Create an inline keyboard with a single cancel button
        cancel = telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(strings.menu_cancel,
                                                                               callback_data="cmd_cancel")]])
        # Request from the user the amount of money to be credited manually
        self.bot.send_message(self.chat.id, strings.ask_credit, reply_markup=cancel)
        # Wait for an answer
        reply = self.__wait_for_regex(r"(-? ?[0-9]{1,3}(?:[.,][0-9]{1,2})?)", cancellable=True)
        # Allow the cancellation of the operation
        if isinstance(reply, CancelSignal):
            return
        # Convert the reply to a price object
        price = utils.Price(reply)
        # Ask the user for notes
        self.bot.send_message(self.chat.id, strings.ask_transaction_notes, reply_markup=cancel)
        # Wait for an answer
        reply = self.__wait_for_regex(r"(.*)", cancellable=True)
        # Allow the cancellation of the operation
        if isinstance(reply, CancelSignal):
            return
        # Create a new transaction
        transaction = db.Transaction(user=user,
                                     value=int(price),
                                     provider="Manual",
                                     notes=reply)
        self.session.add(transaction)
        # Change the user credit
        user.recalculate_credit()
        # Commit the changes
        self.session.commit()
        # Notify the user of the credit/debit
        self.bot.send_message(user.user_id,
                              strings.notification_transaction_created.format(transaction=str(transaction)))
        # Notify the admin of the success
        self.bot.send_message(self.chat.id, strings.success_transaction_created.format(transaction=str(transaction)))

    def __help_menu(self):
        """Help menu. Allows the user to ask for assistance, get a guide or see some info about the bot."""
        # Create a keyboard with the user help menu
        keyboard = [[telegram.KeyboardButton(strings.menu_contact_shopkeeper)],
                    [telegram.KeyboardButton(strings.menu_cancel)]]
        # Send the previously created keyboard to the user (ensuring it can be clicked only 1 time)
        self.bot.send_message(self.chat.id,
                              strings.conversation_open_help_menu,
                              reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
        # Wait for a reply from the user
        selection = self.__wait_for_specific_message([strings.menu_guide, strings.menu_contact_shopkeeper,
                                                      strings.menu_cancel])
        # If the user has selected the Guide option...
        if selection == strings.menu_guide:
            # Send them the bot guide
            self.bot.send_message(self.chat.id, strings.help_msg)
        # If the user has selected the Order Status option...
        elif selection == strings.menu_contact_shopkeeper:
            # Find the list of available shopkeepers
            shopkeepers = self.session.query(db.Admin).filter_by(display_on_help=True).join(db.User).all()
            # Create the string
            shopkeepers_string = "\n".join([admin.user.mention() for admin in shopkeepers])
            # Send the message to the user
            self.bot.send_message(self.chat.id, strings.contact_shopkeeper.format(shopkeepers=shopkeepers_string))
        # If the user has selected the Cancel option the function will return immediately

    def __transaction_pages(self):
        """Display the latest transactions, in pages."""
        # Page number
        page = 0
        # Create and send a placeholder message to be populated
        message = self.bot.send_message(self.chat.id, strings.loading_transactions)
        # Loop used to move between pages
        while True:
            # Retrieve the 10 transactions in that page
            transactions = self.session.query(db.Transaction) \
                .order_by(db.Transaction.transaction_id.desc()) \
                .limit(10) \
                .offset(10 * page) \
                .all()
            # Create a list to be converted in inline keyboard markup
            inline_keyboard_list = [[]]
            # Don't add a previous page button if this is the first page
            if page != 0:
                # Add a previous page button
                inline_keyboard_list[0].append(
                    telegram.InlineKeyboardButton(strings.menu_previous, callback_data="cmd_previous")
                )
            # Don't add a next page button if this is the last page
            if len(transactions) == 10:
                # Add a next page button
                inline_keyboard_list[0].append(
                    telegram.InlineKeyboardButton(strings.menu_next, callback_data="cmd_next")
                )
            # Add a Done button
            inline_keyboard_list.append([telegram.InlineKeyboardButton(strings.menu_done, callback_data="cmd_done")])
            # Create the inline keyboard markup
            inline_keyboard = telegram.InlineKeyboardMarkup(inline_keyboard_list)
            # Create the message text
            transactions_string = "\n".join([str(transaction) for transaction in transactions])
            text = strings.transactions_page.format(page=page + 1,
                                                    transactions=transactions_string)
            # Update the previously sent message
            self.bot.edit_message_text(chat_id=self.chat.id, message_id=message.message_id, text=text,
                                       reply_markup=inline_keyboard)
            # Wait for user input
            selection = self.__wait_for_inlinekeyboard_callback()
            # If Previous was selected...
            if selection.data == "cmd_previous" and page != 0:
                # Go back one page
                page -= 1
            # If Next was selected...
            elif selection.data == "cmd_next" and len(transactions) == 10:
                # Go to the next page
                page += 1
            # If Done was selected...
            elif selection.data == "cmd_done":
                # Break the loop
                break

    def __transactions_file(self):
        """Generate a .csv file containing the list of all transactions."""
        # Retrieve all the transactions
        transactions = self.session.query(db.Transaction).order_by(db.Transaction.transaction_id.asc()).all()
        # Create the file if it doesn't exists
        try:
            with open(f"transactions_{self.chat.id}.csv", "x"):
                pass
        except IOError:
            pass
        # Write on the previously created file
        with open(f"transactions_{self.chat.id}.csv", "w") as file:
            # Write an header line
            file.write(f"UserID;"
                       f"TransactionValue;"
                       f"TransactionNotes;"
                       f"Provider;"
                       f"ChargeID;"
                       f"SpecifiedName;"
                       f"SpecifiedPhone;"
                       f"SpecifiedEmail;"
                       f"Refunded?\n")
            # For each transaction; write a new line on file
            for transaction in transactions:
                file.write(f"{transaction.user_id if transaction.user_id is not None else ''};"
                           f"{transaction.value if transaction.value is not None else ''};"
                           f"{transaction.notes if transaction.notes is not None else ''};"
                           f"{transaction.provider if transaction.provider is not None else ''};"
                           f"{transaction.provider_charge_id if transaction.provider_charge_id is not None else ''};"
                           f"{transaction.payment_name if transaction.payment_name is not None else ''};"
                           f"{transaction.payment_phone if transaction.payment_phone is not None else ''};"
                           f"{transaction.payment_email if transaction.payment_email is not None else ''};"
                           f"{transaction.refunded if transaction.refunded is not None else ''}\n")
        # Describe the file to the user
        self.bot.send_message(self.chat.id, strings.csv_caption)
        # Reopen the file for reading
        with open(f"transactions_{self.chat.id}.csv") as file:
            # Send the file via a manual request to Telegram
            requests.post(f"https://api.telegram.org/bot{configloader.config['Telegram']['token']}/sendDocument",
                          files={"document": file},
                          params={"chat_id": self.chat.id,
                                  "parse_mode": "HTML"})
        # Delete the created file
        os.remove(f"transactions_{self.chat.id}.csv")

    def __select_or_create_admin(self):
        """Add an administrator to the bot."""
        # Let the admin select an administrator to promote
        user = self.__user_select()
        # Allow the cancellation of the operation
        if isinstance(user, CancelSignal):
            return None, None
        # Check if the user is already an administrator
        admin = self.session.query(db.Admin).filter_by(user_id=user.user_id).one_or_none()
        # Return if user is not an admin
        if admin is None:
            # Create the keyboard to be sent
            keyboard = telegram.ReplyKeyboardMarkup([[strings.emoji_yes, strings.emoji_no]], one_time_keyboard=True)
            # Ask for confirmation on the admin deletion
            self.bot.send_message(self.chat.id, strings.conversation_confirm_admin_promotion, reply_markup=keyboard)
            # Wait for an answer
            selection = self.__wait_for_specific_message([strings.emoji_yes, strings.emoji_no])
            # Proceed only if the answer is yes
            if selection == strings.emoji_no:
                return None, None
            # Create a new admin
            admin = db.Admin(user=user,
                                edit_products=False,
                                receive_orders=False,
                                create_transactions=False,
                                is_owner=False,
                                display_on_help=False)
            self.session.add(admin)
            self.session.commit()

        return user, admin

    def __add_admin(self):
        """Add an administrator to the bot."""
        user, admin = self.__select_or_create_admin()

        if (user is None or admin is None):
            return

        # Send the empty admin message and record the id
        message = self.bot.send_message(self.chat.id, strings.admin_properties.format(name=str(user)))
        # Start accepting edits
        while True:
            # Create the inline keyboard with the admin status
            inline_keyboard = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton(f"{utils.boolmoji(admin.edit_products)} {strings.prop_edit_products}",
                                                callback_data="toggle_edit_products")],
                [telegram.InlineKeyboardButton(f"{utils.boolmoji(admin.receive_orders)} {strings.prop_receive_orders}",
                                                callback_data="toggle_receive_orders")],
                [telegram.InlineKeyboardButton(
                    f"{utils.boolmoji(admin.create_transactions)} {strings.prop_create_transactions}",
                    callback_data="toggle_create_transactions")],
                [telegram.InlineKeyboardButton(
                    f"{utils.boolmoji(admin.display_on_help)} {strings.prop_display_on_help}",
                    callback_data="toggle_display_on_help")],
                [telegram.InlineKeyboardButton(strings.conversation_admin_dismissal_menu, callback_data="cmd_remove")],
                [telegram.InlineKeyboardButton(strings.menu_done, callback_data="cmd_done")]
            ])
            # Update the inline keyboard
            self.bot.edit_message_reply_markup(message_id=message.message_id,
                                                chat_id=self.chat.id,
                                                reply_markup=inline_keyboard)
            # Wait for an user answer
            callback = self.__wait_for_inlinekeyboard_callback()
            # Toggle the correct property
            if callback.data == "toggle_edit_products":
                admin.edit_products = not admin.edit_products
            elif callback.data == "toggle_receive_orders":
                admin.receive_orders = not admin.receive_orders
            elif callback.data == "toggle_create_transactions":
                admin.create_transactions = not admin.create_transactions
            elif callback.data == "toggle_display_on_help":
                admin.display_on_help = not admin.display_on_help
            elif callback.data == "cmd_remove":
                self.session.query(db.Admin).filter(db.Admin.user_id==user.user_id).delete()
                message = self.bot.send_message(self.chat.id, strings.conversation_confirm_admin_dismissal)
                break
            elif callback.data == "cmd_done":
                break
        self.session.commit()

    def __graceful_stop(self, stop_trigger: StopSignal):
        """Handle the graceful stop of the thread."""
        # If the session has expired...
        if stop_trigger.reason == "timeout":
            # Notify the user that the session has expired and remove the keyboard
            self.bot.send_message(self.chat.id, strings.conversation_expired,
                                  reply_markup=telegram.ReplyKeyboardRemove())
        # If a restart has been requested...
        # Do nothing.
        # Close the database session
        # End the process
        sys.exit(0)
