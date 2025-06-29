from flask import Flask, request, jsonify
import os
import json
import logging
from datetime import datetime, timezone
from pyairtable import Api
import re
import uuid
import time
import random

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration from environment variables
AIRTABLE_PAT = os.getenv("AIRTABLE_PAT")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME", "Responses")
AIRTABLE_ORDERS_TABLE = os.getenv("AIRTABLE_ORDERS_TABLE", "Orders")
HELP_PHONE = os.getenv("HELP_PHONE", "0548118716")

# Airtable setup (optional) - Using new API
airtable_table = None
airtable_orders = None
if AIRTABLE_PAT and AIRTABLE_BASE_ID:
    try:
        api = Api(AIRTABLE_PAT)
        base = api.base(AIRTABLE_BASE_ID)
        airtable_table = base.table(AIRTABLE_TABLE_NAME)
        airtable_orders = base.table(AIRTABLE_ORDERS_TABLE)
        logger.info("Airtable connected")
    except Exception as e:
        logger.error(f"Airtable failed: {e}")

# Menu data
CATEGORIES = ["Chef One", "Tovet", "Dine Inn - KT", "Founn", "Test"]
MENUS = {
    "Chef One": [("Jollof Rice", 35), ("Banku & Tilapia", 40), ("Indomie", 35), ("FriedRice & Chicken", 35)],
    "Tovet": [("Jollof & Chicken", 35), ("FriedRice & Chicken", 35), ("Banku", 30)],
    "Dine Inn - KT": [("FriedRice & Chicken", 35), ("Jollof & Chicken", 35), ("Jollof & Chicken", 30)],
    "Founn": [("Banku & Tilapia", 35), ("FriedRice & Chicken", 35), ("Jollof & Chicken", 35)],
    "Test": [("Coconut",0.2), ("Kivo",0.1)],
}

# In-memory session storage
memory_sessions = {}

def get_airtable_datetime():
    """Get datetime in Airtable-compatible format"""
    return datetime.now().strftime("%Y-%m-%d %H:%M")

def validate_phone_number(phone):
    """Validate phone number - must start with 233"""
    if not phone:
        return False
    
    clean_phone = re.sub(r'[^\d]', '', phone)
    pattern = r'^233[2-9]\d{8}$'
    
    if re.match(pattern, clean_phone):
        return True
        
    return False

def sanitize_input(text):
    """Clean user input"""
    if not text:
        return ""
    return re.sub(r'[<>"\']', '', text.strip())[:200]

def get_session(msisdn):
    """Get user session from memory"""
    if msisdn not in memory_sessions:
        memory_sessions[msisdn] = {
            "state": "MAIN_MENU",
            "cart": [],
            "selected_category": None,
            "selected_item": None,
            "delivery_location": "",
            "custom_order": "",
            "total": 0,
            "order_history": [],
            "session_id": str(uuid.uuid4())
        }
    return memory_sessions[msisdn]

def save_session(msisdn, session):
    """Save user session to memory"""
    memory_sessions[msisdn] = session

def log_to_airtable(msisdn, userid, message, continue_session, state=None, session_id=None):
    """Log to Airtable with multiple message columns"""
    if not airtable_table:
        return
    
    try:
        airtable_table.create({
            "MSISDN": msisdn,
            "USERID": userid,
            "M1": message,
            "ContinueSession": str(continue_session),
            "State": state or "unknown",
            "SessionID": session_id or "unknown",
            "Timestamp": get_airtable_datetime()
        })
        logger.info(f"Logged to Airtable: {msisdn} - {message[:50]}...")
    except Exception as e:
        logger.error(f"Airtable log error: {e}")

def create_order(session, msisdn, order_type="regular"):
    """Create order record"""
    order_id = str(uuid.uuid4())[:8].upper()
    
    if order_type == "custom":
        # Custom order
        items = [{"name": "Custom Order", "description": session["custom_order"], "price": 30, "quantity": 1}]
        total = 30
    else:
        # Regular food order
        items = []
        total_items = 0
        items_total = 0
        
        for item, qty in session["cart"]:
            items.append({"name": item[0], "price": item[1], "quantity": qty})
            total_items += qty
            items_total += item[1] * qty
        
        delivery_fee = 15 + (total_items - 1) * 5 if total_items > 0 else 0
        extra_charge = 2
        total = items_total + delivery_fee + extra_charge
    
    if airtable_orders:
        try:
            airtable_orders.create({
                "OrderID": order_id,
                "MSISDN": msisdn,
                "Items": json.dumps(items),
                "Total": total,
                "DeliveryLocation": session["delivery_location"],
                "OrderType": order_type,
                "Status": "Processing",
                "CreatedAt": get_airtable_datetime()
            })
        except Exception as e:
            logger.error(f"Order log error: {e}")
    
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
    """Main USSD handler"""
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
        
        log_to_airtable(msisdn, user_id, input_text, True, session['state'], session.get('session_id'))
        
        # Handle different states
        if state == "MAIN_MENU":
            response = handle_main_menu(input_text, session, user_id, msisdn)
        elif state == "CATEGORY":
            response = handle_category(input_text, session, user_id, msisdn)
        elif state == "ITEM":
            response = handle_item(input_text, session, user_id, msisdn)
        elif state == "QTY":
            response = handle_quantity(input_text, session, user_id, msisdn)
        elif state == "CART":
            response = handle_cart(input_text, session, user_id, msisdn)
        elif state == "CUSTOM_ORDER":
            response = handle_custom_order(input_text, session, user_id, msisdn)
        elif state == "DELIVERY":
            response = handle_delivery(input_text, session, user_id, msisdn)
        elif state == "CONFIRM":
            response = handle_confirm(input_text, session, user_id, msisdn)
        elif state == "CUSTOM_CONFIRM":
            response = handle_custom_confirm(input_text, session, user_id, msisdn)
        else:
            session["state"] = "MAIN_MENU"
            response = handle_main_menu("", session, user_id, msisdn)
        
        save_session(msisdn, session)
        return response
        
    except Exception as e:
        logger.error(f"USSD error: {e}", exc_info=True)
        return jsonify({"error": "Internal error"}), 500

def handle_main_menu(input_text, session, user_id, msisdn):
    """Handle main menu"""
    msg = "Welcome to FLAP Dish!\n1. Order Food\n2. Custom Order\n3. My Orders\n4. Help\n0. Exit"
    
    if input_text == "" or input_text.startswith("*"):
        pass
    elif input_text == "1":
        session["state"] = "CATEGORY"
        cat_menu = "\n".join([f"{i+1}. {cat}" for i, cat in enumerate(CATEGORIES)])
        msg = f"Select Category:\n{cat_menu}\n#. Back"
    elif input_text == "2":
        session["state"] = "CUSTOM_ORDER"
        msg = "Enter your custom order details (what you want prepared):"
    elif input_text == "3":
        orders = session.get("order_history", [])
        if orders:
            recent = orders[-3:]
            order_lines = [f"{o['order_id']}: GHS {o['total']} ({o.get('order_type', 'regular')})" for o in recent]
            msg = "Recent Orders:\n" + "\n".join(order_lines) + "\n#. Back"
        else:
            msg = "No orders yet.\n#. Back"
    elif input_text == "4":
        msg = f"Call {HELP_PHONE} for help.\n#. Back"
    elif input_text == "0":
        msg = "Thank you for using FLAP Dish!"
        return ussd_response(user_id, msisdn, msg, False)
    elif input_text == "#":
        pass
    else:
        if len(input_text) == 1 and input_text.isdigit():
            msg = "Invalid option.\n" + msg
    
    return ussd_response(user_id, msisdn, msg, True)

def handle_custom_order(input_text, session, user_id, msisdn):
    """Handle custom order input"""
    if input_text == "#":
        session["state"] = "MAIN_MENU"
        return handle_main_menu("", session, user_id, msisdn)
    
    if input_text and len(input_text.strip()) >= 10:
        session["custom_order"] = input_text.strip()
        session["state"] = "DELIVERY"
        msg = "Enter delivery location:"
        return ussd_response(user_id, msisdn, msg, True)
    
    msg = "Enter your custom order details (min 10 characters):\n#. Back"
    return ussd_response(user_id, msisdn, msg, True)

def handle_category(input_text, session, user_id, msisdn):
    """Handle category selection"""
    if input_text == "#":
        session["state"] = "MAIN_MENU"
        return handle_main_menu("", session, user_id, msisdn)
    
    try:
        if input_text in [str(i+1) for i in range(len(CATEGORIES))]:
            cat = CATEGORIES[int(input_text)-1]
            session["selected_category"] = cat
            session["state"] = "ITEM"
            menu = MENUS[cat]
            menu_str = "\n".join([f"{i+1}. {m[0]} - GHS {m[1]}" for i, m in enumerate(menu)])
            msg = f"{cat}:\n{menu_str}\n#. Back"
            return ussd_response(user_id, msisdn, msg, True)
    except (ValueError, IndexError):
        pass
    
    cat_menu = "\n".join([f"{i+1}. {cat}" for i, cat in enumerate(CATEGORIES)])
    msg = f"Select Category:\n{cat_menu}\n#. Back"
    return ussd_response(user_id, msisdn, msg, True)

def handle_item(input_text, session, user_id, msisdn):
    """Handle item selection"""
    if input_text == "#":
        session["state"] = "CATEGORY"
        return handle_category("", session, user_id, msisdn)
    
    cat = session["selected_category"]
    menu = MENUS[cat]
    
    try:
        if input_text in [str(i+1) for i in range(len(menu))]:
            item = menu[int(input_text)-1]
            session["selected_item"] = item
            session["state"] = "QTY"
            msg = f"You selected {item[0]}.\nEnter quantity (1-20):"
            return ussd_response(user_id, msisdn, msg, True)
    except (ValueError, IndexError):
        pass
    
    menu_str = "\n".join([f"{i+1}. {m[0]} - GHS {m[1]}" for i, m in enumerate(menu)])
    msg = f"{cat}:\n{menu_str}\n#. Back"
    return ussd_response(user_id, msisdn, msg, True)

def handle_quantity(input_text, session, user_id, msisdn):
    """Handle quantity input"""
    item = session["selected_item"]
    
    try:
        qty = int(input_text)
        if 1 <= qty <= 20:
            session["cart"].append((item, qty))
            session["state"] = "CART"
            msg = f"{qty} x {item[0]} added to cart.\n1. Add more\n2. Checkout\n#. Cancel"
            return ussd_response(user_id, msisdn, msg, True)
    except ValueError:
        pass
    
    msg = f"You selected {item[0]}.\nEnter quantity (1-20):"
    return ussd_response(user_id, msisdn, msg, True)

def handle_cart(input_text, session, user_id, msisdn):
    """Handle cart operations"""
    if input_text == "1":
        session["state"] = "CATEGORY"
        return handle_category("", session, user_id, msisdn)
    elif input_text == "2":
        session["state"] = "DELIVERY"
        msg = "Enter delivery location:"
        return ussd_response(user_id, msisdn, msg, True)
    elif input_text == "#":
        session["cart"] = []
        session["state"] = "MAIN_MENU"
        return handle_main_menu("", session, user_id, msisdn)
    
    msg = "1. Add more\n2. Checkout\n#. Cancel"
    return ussd_response(user_id, msisdn, msg, True)

def handle_delivery(input_text, session, user_id, msisdn):
    """Handle delivery location"""
    if input_text and len(input_text.strip()) >= 3:
        session["delivery_location"] = input_text
        
        # Check if it's a custom order or regular order
        if session.get("custom_order"):
            session["state"] = "CUSTOM_CONFIRM"
            return show_custom_confirmation(session, user_id, msisdn)
        else:
            session["state"] = "CONFIRM"
            return show_confirmation(session, user_id, msisdn)
    
    msg = "Enter delivery location (min 3 chars):"
    return ussd_response(user_id, msisdn, msg, True)

def handle_confirm(input_text, session, user_id, msisdn):
    """Handle regular order confirmation"""
    if input_text == "2":
        session["cart"] = []
        session["state"] = "MAIN_MENU"
        return handle_main_menu("", session, user_id, msisdn)
    elif input_text == "1":
        order_id, total = create_order(session, msisdn, "regular")
        session["cart"] = []
        session["state"] = "MAIN_MENU"
        
        msg = f"Order #{order_id} created!\n\nPlease dial *415*1738# and pay GHS {total} for order processing.\n\nThank you!"
        return ussd_response(user_id, msisdn, msg, False)
    
    return show_confirmation(session, user_id, msisdn)

def handle_custom_confirm(input_text, session, user_id, msisdn):
    """Handle custom order confirmation"""
    if input_text == "2":
        session["custom_order"] = ""
        session["state"] = "MAIN_MENU"
        return handle_main_menu("", session, user_id, msisdn)
    elif input_text == "1":
        order_id, total = create_order(session, msisdn, "custom")
        session["custom_order"] = ""
        session["state"] = "MAIN_MENU"
        
        msg = f"Custom Order #{order_id} created!\n\nPlease dial *415*1738# and pay GHS 30 for delivery.\n\nThank you!"
        return ussd_response(user_id, msisdn, msg, False)
    
    return show_custom_confirmation(session, user_id, msisdn)

def show_confirmation(session, user_id, msisdn):
    """Show regular order confirmation"""
    lines = [f"{qty} x {item[0]} - GHS {item[1]*qty}" for item, qty in session["cart"]]
    item_count = sum(qty for item, qty in session["cart"])
    delivery_fee = 15 + (item_count - 1) * 5 if item_count > 0 else 0
    extra_charge = 2
    items_total = sum(item[1]*qty for item, qty in session["cart"])
    total = items_total + delivery_fee + extra_charge
    session["total"] = total
    
    msg = (
        "Order Summary:\n" + "\n".join(lines) +
        f"\nDelivery: GHS {delivery_fee}" +
        f"\nService: GHS {extra_charge}" +
        f"\nLocation: {session['delivery_location']}" +
        f"\nTotal: GHS {total}\n\n1. Confirm\n2. Cancel"
    )
    return ussd_response(user_id, msisdn, msg, True)

def show_custom_confirmation(session, user_id, msisdn):
    """Show custom order confirmation"""
    msg = (
        "Custom Order Summary:\n" +
        f"Request: {session['custom_order'][:50]}..." +
        f"\nLocation: {session['delivery_location']}" +
        f"\nDelivery Fee: GHS 30" +
        f"\n\n1. Confirm\n2. Cancel"
    )
    return ussd_response(user_id, msisdn, msg, True)

def ussd_response(userid, msisdn, msg, continue_session=True):
    """Generate USSD response"""
    truncated_msg = msg[:160]  # Increased limit for better message display
    
    log_to_airtable(msisdn, userid, truncated_msg, continue_session)
    
    logger.info(f"Response to {msisdn}: {truncated_msg[:50]}...")
    
    return jsonify({
        "USERID": userid,
        "MSISDN": msisdn,
        "MSG": truncated_msg,
        "MSGTYPE": bool(continue_session)
    })

@app.route("/health", methods=["GET"])
def health_check():
    """Health check"""
    return jsonify({
        "status": "healthy",
        "timestamp": get_airtable_datetime(),
        "airtable": "connected" if airtable_table else "disabled"
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting USSD Food Ordering on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)
