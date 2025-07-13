# ------ dependencies -----
from flask import Flask, request, jsonify
import os
import json
import logging
from datetime import datetime
from pyairtable import Api
import re
import uuid
import urllib.request
import urllib.parse

# --- Firebase imports ---
import firebase_admin
from firebase_admin import credentials, firestore

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Environment variables
AIRTABLE_PAT = os.getenv("AIRTABLE_PAT")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_ORDERS_TABLE = os.getenv("AIRTABLE_ORDERS_TABLE", "Orders")
SUPPORT_PHONE = os.getenv("SUPPORT_PHONE")

BULK_SMS_API_KEY = os.getenv("BULK_SMS_API_KEY")  #sms api (bulk sms ghana) 
BULK_SMS_SENDER_ID = os.getenv("BULK_SMS_SENDER_ID")

# --- Firebase initialization ---
FIREBASE_CREDENTIALS_JSON = os.getenv("FIREBASE_CREDENTIALS_JSON")
firebase_db = None

if FIREBASE_CREDENTIALS_JSON:
    try:
        creds_path = "/tmp/firebase_creds.json"
        with open(creds_path, "w") as f:
            f.write(FIREBASE_CREDENTIALS_JSON)
        cred = credentials.Certificate(creds_path)
        firebase_admin.initialize_app(cred)
        firebase_db = firestore.client()
        logger.info("Firebase initialized")
    except Exception as e:
        logger.error(f"Firebase init error: {e}")

def log_to_firebase(msisdn, userid, message, continue_session, state=None, session_id=None):
    if not firebase_db:
        return
    try:
        doc_ref = firebase_db.collection("ussd_responses").document()
        doc_ref.set({
            "MSISDN": msisdn,
            "USERID": userid,
            "M1": message,
            "ContinueSession": str(continue_session),
            "State": state or "unknown",
            "SessionID": session_id or "unknown",
            "Timestamp": get_airtable_datetime()
        })
        logger.info(f"Logged to Firebase: {msisdn} - {message[:50]}...")
    except Exception as e:
        logger.error(f"Firebase log error: {e}")

def send_sms_ghana(phone_number, message):
    params = {
        'key': BULK_SMS_API_KEY,
        'to': phone_number,
        'msg': message,
        'sender_id': BULK_SMS_SENDER_ID
    }
    url = 'http://clientlogin.bulksmsgh.com/smsapi?' + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url) as response:
            content = response.read().decode('utf-8')
            code = content.strip()
            logger.info(f"SMS API response: {code}")
            return code == '1000'
    except Exception as e:
        logger.error(f"SMS sending failed: {e}")
    return False

# Airtable setup (orders only)
airtable_orders = None
if AIRTABLE_PAT and AIRTABLE_BASE_ID:
    try:
        api = Api(AIRTABLE_PAT)
        base = api.base(AIRTABLE_BASE_ID)
        airtable_orders = base.table(AIRTABLE_ORDERS_TABLE)
        logger.info("Airtable connected for orders")
    except Exception as e:
        logger.error(f"Airtable failed: {e}")

CATEGORIES = [
    "Chef One",
    "Eno's Kitchen",
    "Tovet",
    "Dine Inn - KT",
    "Founn",
    "KFC - Tarkwa",
    "Pizzaman"
]

MENUS = {
    "Chef One": [("Jollof Rice", 35), ("Banku & Tilapia", 40), ("Indomie", 35), ("FriedRice & Chicken", 35)],
    "Eno's Kitchen": [("Jollof Rice", 35), ("Banku & Tilapia", 40), ("FriedRice & Chicken", 35)],
    "Tovet": [("Jollof & Chicken", 35), ("FriedRice & Chicken", 35), ("Banku", 40)],
    "Dine Inn - KT": [("FriedRice & Chicken", 35), ("Jollof & Chicken", 35), ("Jollof & Chicken", 35)],
    "Founn": [("Banku & Tilapia", 35), ("FriedRice & Chicken", 35), ("Jollof & Chicken", 35)],
    "KFC - Tarkwa": [("15 Pieces Chicken", 427), ("Streetwise 2-Chips", 88), ("Streetwise 3-Rice", 112)],
    "Pizzaman": [("Triple b-double Pizza", 290), ("Dukeman-small Pizza", 150), ("Chibella-double Pizza", 290)]
}

CATEGORY_DELIVERY_FEES = {
    "Chef One": 15,
    "Eno's Kitchen": 15,
    "Tovet": 15,
    "Dine Inn - KT": 15,
    "Founn": 15,
    "KFC - Tarkwa": 25,  # Will use special area logic below
    "Pizzaman": 15,
    "Custom": 30
}
DEFAULT_DELIVERY_FEE = 15
GAS_DELIVERY_FEE = 30

KFC_TARKWA_DELIVERY_PRICES = {
    "tarkwa central": 30,
    "tna": 30,
    "university": 30,
    "aboso": 30,
    "other": 30
}

memory_sessions = {}

GAS_SIZES = [("3kg", 20), ("6kg", 30), ("12.5kg", 50)]

CUSTOM_ORDER_MENUS = [
    "Grocery (Ransbet)",
    "Pickup",
    "Custom food order",
    "Other"
]

def get_airtable_datetime():
    return datetime.now().strftime("%Y-%m-%d %H:%M")

def validate_phone_number(phone):
    if not phone:
        return False
    clean_phone = re.sub(r'[^\d]', '', phone)
    pattern = r'^233[2-9]\d{8}$'
    return bool(re.match(pattern, clean_phone))

def sanitize_input(text):
    if not text:
        return ""
    return re.sub(r'[<>"\']', '', text.strip())[:200]

def get_session(msisdn):
    if msisdn not in memory_sessions:
        memory_sessions[msisdn] = {
            "state": "MAIN_MENU",
            "cart": [],
            "selected_category": None,
            "selected_item": None,
            "delivery_location": "",
            "custom_order": "",
            "custom_order_type": "",
            "total": 0,
            "order_history": [],
            "session_id": str(uuid.uuid4()),
            "discount_code": None,
            "discount_amount": 0,
            "delivery_note": ""
        }
    # Ensure delivery_note key is always present
    if "delivery_note" not in memory_sessions[msisdn]:
        memory_sessions[msisdn]["delivery_note"] = ""
    return memory_sessions[msisdn]

def save_session(msisdn, session):
    memory_sessions[msisdn] = session

def log_to_airtable_order(msisdn, userid, items, total, delivery_location, order_type, order_id, delivery_note=""):
    if not airtable_orders:
        return
    try:
        airtable_orders.create({
            "OrderID": order_id,
            "MSISDN": msisdn,
            "USERID": userid,
            "Items": json.dumps(items),
            "Total": total,
            "DeliveryLocation": delivery_location,
            "OrderType": order_type,
            "Status": "Processing",
            "CreatedAt": get_airtable_datetime(),
            "DeliveryNote": delivery_note
        })
        logger.info(f"Order logged to Airtable: {msisdn} - {order_id}")
    except Exception as e:
        logger.error(f"Airtable order log error: {e}")

def get_delivery_fee(session):
    vendor = session.get("selected_category", "")
    location = session.get("delivery_location", "").strip().lower()
    if vendor == "KFC - Tarkwa":
        for loc, fee in KFC_TARKWA_DELIVERY_PRICES.items():
            if loc != "other" and loc in location:
                return fee
        return KFC_TARKWA_DELIVERY_PRICES["other"]
    elif vendor:
        return CATEGORY_DELIVERY_FEES.get(vendor, DEFAULT_DELIVERY_FEE)
    elif session.get("custom_order"):
        return CATEGORY_DELIVERY_FEES.get("Custom", 30)
    else:
        return DEFAULT_DELIVERY_FEE

def create_order(session, msisdn, order_type="regular", user_id=""):
    order_id = str(uuid.uuid4())[:8].upper()
    delivery_note = session.get("delivery_note", "")
    if order_type == "custom":
        items = [{
            "name": f"Custom Order ({session.get('custom_order_type', 'Other')})",
            "description": session["custom_order"],
            "price": CATEGORY_DELIVERY_FEES.get("Custom", 30),
            "quantity": 1,
            "category": "Custom"
        }]
        total = CATEGORY_DELIVERY_FEES.get("Custom", 30)
    elif order_type == "gas_filling":
        gas_size = session.get("selected_gas", ("Unknown", 0))
        gas_amount = session.get("gas_fill_amount", 0)
        delivery_fee = GAS_DELIVERY_FEE
        items = [{
            "name": f"Gas Filling - {gas_size[0]}",
            "price": gas_amount,
            "quantity": 1,
            "category": "Gas Filling"
        }]
        total = gas_amount + delivery_fee
        log_to_airtable_order(
            msisdn, user_id, items, total,
            session.get("gas_location", ""),
            order_type, order_id,
            delivery_note
        )
        session["order_history"].append({
            "order_id": order_id,
            "total": total,
            "order_type": order_type,
            "created_at": get_airtable_datetime()
        })
        return order_id, total
    else:
        items = []
        items_total = 0
        for item, qty, category in session["cart"]:
            items.append({
                "name": item[0],
                "price": item[1],
                "quantity": qty,
                "category": category
            })
            items_total += item[1] * qty
        delivery_fee = get_delivery_fee(session)
        extra_charge = 4
        total = items_total + delivery_fee + extra_charge
        if session.get("discount_amount"):
            total -= session["discount_amount"]
            if total < 0:
                total = 0

    log_to_airtable_order(
        msisdn, user_id, items, total,
        session.get("delivery_location", ""),
        order_type, order_id,
        delivery_note
    )

    session["order_history"].append({
        "order_id": order_id,
        "total": total,
        "order_type": order_type,
        "created_at": get_airtable_datetime()
    })
    return order_id, total

@app.route("/", methods=["POST"])
@app.route("/ussd", methods=["POST"])
def ussd_handler():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Invalid request"}), 400
        msisdn = data.get("MSISDN")
        input_text = sanitize_input(data.get("USERDATA", ""))
        user_id = data.get("USERID", "NALOTest")
        logger.info(f"Received MSISDN: '{msisdn}' (type: {type(msisdn)})")
        logger.info(f"Full request data: {data}")

        if not msisdn:
            logger.error("No MSISDN provided in request")
            return ussd_response(user_id, "unknown", "No phone number provided.", False)
        if not validate_phone_number(msisdn):
            logger.error(f"Phone validation failed for: '{msisdn}'")
            return ussd_response(user_id, msisdn, f"Phone must start with 233. Got: {msisdn}", False)
        session = get_session(msisdn)
        state = session["state"]
        logger.info(f"USSD: {msisdn}, State: {state}, Input: '{input_text}'")
        log_to_firebase(msisdn, user_id, input_text, True, session['state'], session.get('session_id'))
        if state == "MAIN_MENU":
            response = handle_main_menu(input_text, session, user_id, msisdn)
        elif state == "GAS_SIZE":
            response = handle_gas_size(input_text, session, user_id, msisdn)
        elif state == "GAS_AMOUNT":
            response = handle_gas_amount(input_text, session, user_id, msisdn)
        elif state == "GAS_LOCATION":
            response = handle_gas_location(input_text, session, user_id, msisdn)
        elif state == "GAS_CONFIRM":
            response = handle_gas_confirm(input_text, session, user_id, msisdn)
        elif state == "CATEGORY":
            response = handle_category(input_text, session, user_id, msisdn)
        elif state == "ITEM":
            response = handle_item(input_text, session, user_id, msisdn)
        elif state == "QTY":
            response = handle_quantity(input_text, session, user_id, msisdn)
        elif state == "CART":
            response = handle_cart(input_text, session, user_id, msisdn)
        elif state == "CUSTOM_ORDER_TYPE":
            response = handle_custom_order_type(input_text, session, user_id, msisdn)
        elif state == "CUSTOM_ORDER":
            response = handle_custom_order(input_text, session, user_id, msisdn)
        elif state == "DELIVERY":
            response = handle_delivery(input_text, session, user_id, msisdn)
        elif state == "DISCOUNT_ASK":
            response = handle_discount_ask(input_text, session, user_id, msisdn)
        elif state == "DISCOUNT_ENTER":
            response = handle_discount_enter(input_text, session, user_id, msisdn)
        elif state == "CONFIRM":
            response = handle_confirm(input_text, session, user_id, msisdn)
        elif state == "CUSTOM_CONFIRM":
            response = handle_custom_confirm(input_text, session, user_id, msisdn)
        elif state == "DELIVERY_NOTE":
            response = handle_delivery_note(input_text, session, user_id, msisdn)
        else:
            session["state"] = "MAIN_MENU"
            response = handle_main_menu("", session, user_id, msisdn)
        save_session(msisdn, session)
        return response
    except Exception as e:
        logger.error(f"USSD error: {e}", exc_info=True)
        return jsonify({"error": "Internal error"}), 500

def handle_main_menu(input_text, session, user_id, msisdn):
    msg = "Welcome to FLAP Dish!\n1. Order Food\n2. Gas Filling\n3. Custom Order\n4. My Orders\n5. Help\n6. Campus Sellers\n0. Exit"
    if input_text == "" or input_text.startswith("*") or input_text == "#":
        pass
    elif input_text == "1":
        session["state"] = "CATEGORY"
        cat_menu = "\n".join([f"{i+1}. {cat}" for i, cat in enumerate(CATEGORIES)])
        msg = f"Select Vendor:\n{cat_menu}\n#. Back"
    elif input_text == "2":
        session["state"] = "GAS_SIZE"
        gas_size_menu = "\n".join([f"{i+1}. {s[0]} (min GHS {s[1]})" for i, s in enumerate(GAS_SIZES)])
        msg = f"Select gas cylinder size:\n{gas_size_menu}\n#. Back"
    elif input_text == "3":
        session["state"] = "CUSTOM_ORDER_TYPE"
        custom_menu = "\n".join([f"{i+1}. {opt}" for i, opt in enumerate(CUSTOM_ORDER_MENUS)])
        msg = f"Select a custom order type:\n{custom_menu}\n#. Back"
    elif input_text == "4":
        orders = session.get("order_history", [])
        if orders:
            recent = orders[-3:]
            order_lines = [f"{o['order_id']}: GHS {o['total']} ({o.get('order_type', 'regular')})" for o in recent]
            msg = "Recent Orders:\n" + "\n".join(order_lines) + "\n#. Back:"
        else:
            msg = "No orders yet.\n#. Back:"
    elif input_text == "5":
        msg = f"Call {SUPPORT_PHONE} for help.\n#. Back:"
    elif input_text == "6":
        msg = "Coming Soon! \n#. Back:"
    elif input_text == "0":
        msg = "Thank you for using FLAP Dish!"
        return ussd_response(user_id, msisdn, msg, False)
    else:
        msg = "Invalid option.\n" + msg
    return ussd_response(user_id, msisdn, msg, True)

def handle_gas_size(input_text, session, user_id, msisdn):
    if input_text == "#":
        session["state"] = "MAIN_MENU"
        return handle_main_menu("", session, user_id, msisdn)
    idxs = [str(i+1) for i in range(len(GAS_SIZES))]
    if input_text in idxs:
        selected = GAS_SIZES[int(input_text)-1]
        session["selected_gas"] = selected
        session["state"] = "GAS_AMOUNT"
        msg = f"{selected[0]} selected. How much do you want to fill? (in cedis)\n#. Back"
        return ussd_response(user_id, msisdn, msg, True)
    gas_size_menu = "\n".join([f"{i+1}. {s[0]} (min GHS {s[1]})" for i, s in enumerate(GAS_SIZES)])
    msg = f"Select gas cylinder size:\n{gas_size_menu}\n#. Back"
    return ussd_response(user_id, msisdn, msg, True)

def handle_gas_amount(input_text, session, user_id, msisdn):
    if input_text == "#":
        session["state"] = "GAS_SIZE"
        return handle_gas_size("", session, user_id, msisdn)
    try:
        amount = int(input_text)
        min_amount = session["selected_gas"][1]
        if amount >= min_amount:
            session["gas_fill_amount"] = amount
            session["state"] = "GAS_LOCATION"
            msg = "Enter delivery location:\n#. Back"
            return ussd_response(user_id, msisdn, msg, True)
        else:
            msg = f"Minimum for {session['selected_gas'][0]} is GHS {min_amount}. Enter amount (in cedis):\n#. Back"
            return ussd_response(user_id, msisdn, msg, True)
    except:
        msg = "Please enter a valid amount in cedis:\n#. Back"
        return ussd_response(user_id, msisdn, msg, True)

def handle_gas_location(input_text, session, user_id, msisdn):
    if input_text == "#":
        session["state"] = "GAS_AMOUNT"
        return handle_gas_amount("", session, user_id, msisdn)
    if input_text and len(input_text.strip()) >= 3:
        session["gas_location"] = input_text.strip()
        session["state"] = "GAS_CONFIRM"
        return show_gas_confirmation(session, user_id, msisdn)
    msg = "Enter delivery location (min 3 chars):\n#. Back"
    return ussd_response(user_id, msisdn, msg, True)

def show_gas_confirmation(session, user_id, msisdn):
    size, min_price = session["selected_gas"]
    fill_amount = session["gas_fill_amount"]
    location = session["gas_location"]
    delivery_fee = GAS_DELIVERY_FEE
    total = fill_amount + delivery_fee
    session["total"] = total
    msg = (
        f"{size} gas\nTop-up: GHS {fill_amount}\n"
        f"Location: {location}\n"
        f"Delivery Fee: GHS {delivery_fee}\n"
        f"Total: GHS {total}\n"
        "1. Confirm\n2. Cancel"
    )
    return ussd_response(user_id, msisdn, msg, True)

def handle_gas_confirm(input_text, session, user_id, msisdn):
    if input_text == "2":
        session.pop("selected_gas", None)
        session.pop("gas_fill_amount", None)
        session.pop("gas_location", None)
        session["state"] = "MAIN_MENU"
        return handle_main_menu("", session, user_id, msisdn)
    elif input_text == "1":
        session["state"] = "DELIVERY_NOTE"
        msg = "Enter delivery note for rider (optional, max 100 chars). Or press 0 to skip:"
        return ussd_response(user_id, msisdn, msg, True)
    return show_gas_confirmation(session, user_id, msisdn)

def handle_custom_order_type(input_text, session, user_id, msisdn):
    if input_text == "#":
        session["state"] = "MAIN_MENU"
        return handle_main_menu("", session, user_id, msisdn)
    idxs = [str(i+1) for i in range(len(CUSTOM_ORDER_MENUS))]
    if input_text in idxs:
        selected_type = CUSTOM_ORDER_MENUS[int(input_text)-1]
        session["custom_order_type"] = selected_type
        session["state"] = "CUSTOM_ORDER"
        msg = f"Enter details for '{selected_type}':\n#. Back"
        return ussd_response(user_id, msisdn, msg, True)
    custom_menu = "\n".join([f"{i+1}. {opt}" for i, opt in enumerate(CUSTOM_ORDER_MENUS)])
    msg = f"Select a custom order type:\n{custom_menu}\n#. Back"
    return ussd_response(user_id, msisdn, msg, True)

def handle_category(input_text, session, user_id, msisdn):
    if input_text == "#":
        session["state"] = "MAIN_MENU"
        return handle_main_menu("", session, user_id, msisdn)
    idxs = [str(i+1) for i in range(len(CATEGORIES))]
    if input_text in idxs:
        cat = CATEGORIES[int(input_text)-1]
        session["selected_category"] = cat
        session["state"] = "ITEM"
        menu = MENUS[cat]
        menu_str = "\n".join([f"{i+1}. {m[0]} - GHS {m[1]}" for i, m in enumerate(menu)])
        msg = f"{cat} Menu:\n{menu_str}\n#. Back:"
        return ussd_response(user_id, msisdn, msg, True)
    cat_menu = "\n".join([f"{i+1}. {cat}" for i, cat in enumerate(CATEGORIES)])
    msg = f"Select Vendor:\n{cat_menu}\n#. Back:"
    return ussd_response(user_id, msisdn, msg, True)

def handle_item(input_text, session, user_id, msisdn):
    if input_text == "#":
        session["state"] = "CATEGORY"
        return handle_category("", session, user_id, msisdn)
    cat = session["selected_category"]
    menu = MENUS[cat]
    idxs = [str(i+1) for i in range(len(menu))]
    if input_text in idxs:
        item = menu[int(input_text)-1]
        session["selected_item"] = item
        session["state"] = "QTY"
        msg = f"{item[0]} selected.\nEnter quantity (1-20):\n#. Back"
        return ussd_response(user_id, msisdn, msg, True)
    menu_str = "\n".join([f"{i+1}. {m[0]} - GHS {m[1]}" for i, m in enumerate(menu)])
    msg = f"{cat} Menu:\n{menu_str}\n#. Back:"
    return ussd_response(user_id, msisdn, msg, True)

def handle_quantity(input_text, session, user_id, msisdn):
    item = session["selected_item"]
    category = session.get("selected_category", "Unknown")
    if input_text == "#":
        session["state"] = "ITEM"
        return handle_item("", session, user_id, msisdn)
    try:
        qty = int(input_text)
        if 1 <= qty <= 20:
            session["cart"].append((item, qty, category))
            session["state"] = "CART"
            msg = f"{qty} x {item[0]} added to cart.\n1. Add more\n2. Checkout\n#. Cancel:"
            return ussd_response(user_id, msisdn, msg, True)
    except ValueError:
        pass
    msg = f"{item[0]} selected.\nEnter quantity (1-20):\n#. Back"
    return ussd_response(user_id, msisdn, msg, True)

def handle_cart(input_text, session, user_id, msisdn):
    if input_text == "1":
        session["state"] = "CATEGORY"
        return handle_category("", session, user_id, msisdn)
    elif input_text == "2":
        session["state"] = "DELIVERY"
        msg = "Enter delivery location:\n#. Back"
        return ussd_response(user_id, msisdn, msg, True)
    elif input_text == "#":
        session["cart"] = []
        session["state"] = "MAIN_MENU"
        return handle_main_menu("", session, user_id, msisdn)
    msg = "1. Add more\n2. Checkout\n#. Cancel:"
    return ussd_response(user_id, msisdn, msg, True)

def handle_delivery(input_text, session, user_id, msisdn):
    if input_text == "#":
        session["state"] = "CART" if not session.get("custom_order") else "CUSTOM_ORDER"
        return handle_cart("", session, user_id, msisdn) if not session.get("custom_order") else handle_custom_order("", session, user_id, msisdn)
    if input_text and len(input_text.strip()) >= 3:
        session["delivery_location"] = input_text
        if session.get("custom_order"):
            session["state"] = "CUSTOM_CONFIRM"
            return show_custom_confirmation(session, user_id, msisdn)
        else:
            return show_confirmation(session, user_id, msisdn)
    msg = "Enter delivery location (min 3 chars):\n#. Back"
    return ussd_response(user_id, msisdn, msg, True)

def show_confirmation(session, user_id, msisdn):
    cart = session["cart"]
    delivery_fee = get_delivery_fee(session)
    extra_charge = 4
    items_total = sum(item[1]*qty for item, qty, cat in cart)
    total = items_total + delivery_fee + extra_charge
    session["total"] = total
    lines = []
    for idx, (item, qty, cat) in enumerate(cart):
        if idx < 2:
            lines.append(f"{qty}x{item[0]}")
    if len(cart) > 2:
        lines.append(f"+{len(cart)-2} more")
    items_line = ", ".join(lines)
    msg = (
        f"{items_line}\nDelivery: GHS {delivery_fee} service: GHS {extra_charge}\n"
        f"Location: {session['delivery_location']}\nTotal: GHS {total}\n"
        "Discount code?\n1. Yes\n2. No:"
    )
    session["state"] = "DISCOUNT_ASK"
    return ussd_response(user_id, msisdn, msg, True)

def handle_discount_ask(input_text, session, user_id, msisdn):
    if input_text == "1":
        session["state"] = "DISCOUNT_ENTER"
        msg = "Enter your discount code:\n#. Back"
        return ussd_response(user_id, msisdn, msg, True)
    elif input_text == "2":
        session["discount_code"] = None
        session["discount_amount"] = 0
        session["state"] = "CONFIRM"
        return show_final_confirmation(session, user_id, msisdn)
    else:
        msg = "Discount code?\n1. Yes\n2. No"
        return ussd_response(user_id, msisdn, msg, True)

def handle_discount_enter(input_text, session, user_id, msisdn):
    code = input_text.strip().upper()
    discount_dict = {"FLAP10": 7, "VOU": 5, "GH": 12}
    if code in discount_dict:
        session["discount_code"] = code
        session["discount_amount"] = discount_dict[code]
        msg = f"Discount applied: GHS {discount_dict[code]} off!"
        session["state"] = "CONFIRM"
        return show_final_confirmation(session, user_id, msisdn, discount_applied_msg=msg)
    elif code == "0" or code == "#":
        session["discount_code"] = None
        session["discount_amount"] = 0
        session["state"] = "CONFIRM"
        return show_final_confirmation(session, user_id, msisdn)
    else:
        msg = "Invalid code. Try again or enter 0 to skip:\n#. Back"
        return ussd_response(user_id, msisdn, msg, True)

def show_final_confirmation(session, user_id, msisdn, discount_applied_msg=None):
    cart = session["cart"]
    delivery_fee = get_delivery_fee(session)
    extra_charge = 4
    items_total = sum(item[1]*qty for item, qty, cat in cart)
    total = items_total + delivery_fee + extra_charge
    if session.get("discount_amount"):
        total -= session["discount_amount"]
        if total < 0:
            total = 0
    session["total"] = total
    lines = []
    for idx, (item, qty, cat) in enumerate(cart):
        if idx < 2:
            lines.append(f"{qty}x{item[0]}")
    if len(cart) > 2:
        lines.append(f"+{len(cart)-2} more")
    items_line = ", ".join(lines)
    msg = ""
    if discount_applied_msg:
        msg += discount_applied_msg + "\n"
    msg += (
        f"{items_line}\nDelivery: GHS {delivery_fee} Service: GHS {extra_charge}"
    )
    if session.get("discount_code"):
        msg += f" Discount:-{session['discount_amount']}"
    msg += (
        f"\nLocation:{session['delivery_location']}\nTotal: GHS {total}\n"
        "1. Confirm\n2. Cancel"
    )
    return ussd_response(user_id, msisdn, msg, True)

def handle_confirm(input_text, session, user_id, msisdn):
    if input_text == "2":
        session["cart"] = []
        session["state"] = "MAIN_MENU"
        session["discount_code"] = None
        session["discount_amount"] = 0
        session["delivery_note"] = ""
        return handle_main_menu("", session, user_id, msisdn)
    elif input_text == "1":
        session["state"] = "DELIVERY_NOTE"
        msg = "Enter delivery note for rider (optional, max 100 chars). Or press 0 to skip:"
        return ussd_response(user_id, msisdn, msg, True)
    return show_final_confirmation(session, user_id, msisdn)

def handle_custom_order(input_text, session, user_id, msisdn):
    if input_text == "#":
        session["state"] = "CUSTOM_ORDER_TYPE"
        return handle_custom_order_type("", session, user_id, msisdn)
    if input_text and len(input_text.strip()) >= 10:
        session["custom_order"] = input_text.strip()
        session["state"] = "DELIVERY"
        msg = "Enter delivery location:\n#. Back"
        return ussd_response(user_id, msisdn, msg, True)
    msg = "Enter custom order details (min 10 chars):\n#. Back"
    return ussd_response(user_id, msisdn, msg, True)

def handle_custom_confirm(input_text, session, user_id, msisdn):
    if input_text == "2":
        session["custom_order"] = ""
        session["custom_order_type"] = ""
        session["state"] = "MAIN_MENU"
        session["delivery_note"] = ""
        return handle_main_menu("", session, user_id, msisdn)
    elif input_text == "1":
        session["state"] = "DELIVERY_NOTE"
        msg = "Enter delivery note for rider (optional, max 100 chars). Or press 0 to skip:"
        return ussd_response(user_id, msisdn, msg, True)
    return show_custom_confirmation(session, user_id, msisdn)

def show_custom_confirmation(session, user_id, msisdn):
    summary = session['custom_order'][:40]
    order_type = session.get("custom_order_type", "Other")
    delivery_fee = CATEGORY_DELIVERY_FEES.get("Custom", 30)
    msg = (
        f"Custom Order ({order_type}):\n{summary}...\n"
        f"Location:{session['delivery_location']}\nDelivery: GHS{delivery_fee}\n"
        "1. Confirm\n2. Cancel"
    )
    return ussd_response(user_id, msisdn, msg, True)

def handle_delivery_note(input_text, session, user_id, msisdn):
    note = input_text.strip()
    if note == "0" or not note:
        session["delivery_note"] = ""
    else:
        session["delivery_note"] = note[:100]
    # Determine order type and finalize order
    msg = ""
    if session.get("custom_order"):
        order_id, total = create_order(session, msisdn, "custom", user_id)
        sms_msg = f"Your FLAP Dish custom order #{order_id} has been received! Please dial *415*1738# and pay GHS {total} to process your order. Thank you!\nNote: {session['delivery_note']}" if session['delivery_note'] else f"Your FLAP Dish custom order #{order_id} has been received! Please dial *415*1738# and pay GHS {total} to process your order. Thank you!"
        send_sms_ghana(msisdn, sms_msg)
        msg = f"Custom Order #{order_id} created!\nPlease dial *415*1738# and pay GHS {total} for delivery.\nThank you!"
        session["custom_order"] = ""
        session["custom_order_type"] = ""
    elif session.get("gas_fill_amount"):
        order_id, total = create_order(session, msisdn, "gas_filling", user_id)
        sms_msg = f"Your Gas Filling order #{order_id} received! Pay GHS {total} to *415*1738# to process your order.\nNote: {session['delivery_note']}" if session['delivery_note'] else f"Your Gas Filling order #{order_id} received! Pay GHS {total} to *415*1738# to process your order."
        send_sms_ghana(msisdn, sms_msg)
        msg = f"Order #{order_id} created!\nPay GHS {total} to *415*1738# for processing.\nThank you!"
        session.pop("selected_gas", None)
        session.pop("gas_fill_amount", None)
        session.pop("gas_location", None)
    else:
        order_id, total = create_order(session, msisdn, "regular", user_id)
        sms_msg = f"Your order #{order_id} has been received! Please dial *415*1738# and pay GHS {total} to process your order. Thank you!\nNote: {session['delivery_note']}" if session['delivery_note'] else f"Your order #{order_id} has been received! Please dial *415*1738# and pay GHS {total} to process your order. Thank you!"
        send_sms_ghana(msisdn, sms_msg)
        msg = f"Order #{order_id} created!\nPlease dial *415*1738# and pay GHS {total} for order processing.\nThank you!"
        session["cart"] = []
        session["discount_code"] = None
        session["discount_amount"] = 0
    session["delivery_note"] = ""
    session["state"] = "MAIN_MENU"
    return ussd_response(user_id, msisdn, msg, False)

def ussd_response(userid, msisdn, msg, continue_session=True):
    truncated_msg = msg[:160]
    log_to_firebase(msisdn, userid, truncated_msg, continue_session)
    logger.info(f"Response to {msisdn}: {truncated_msg[:50]}...")
    return jsonify({
        "USERID": userid,
        "MSISDN": msisdn,
        "MSG": truncated_msg,
        "MSGTYPE": bool(continue_session)
    })

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({
        "status": "healthy",
        "timestamp": get_airtable_datetime(),
        "airtable": "connected" if airtable_orders else "disabled"
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting USSD Food Ordering on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)
