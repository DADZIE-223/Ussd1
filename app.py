from flask import Flask, request, jsonify
import os
import json
import logging
from datetime import datetime
from pyairtable import Table
import re
import uuid

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration from environment variables
AIRTABLE_PAT = os.getenv("AIRTABLE_PAT")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME", "Responses")
AIRTABLE_ORDERS_TABLE = os.getenv("AIRTABLE_ORDERS_TABLE", "Orders")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
HELP_PHONE = os.getenv("HELP_PHONE", "0548118716")

# Airtable setup (optional)
airtable_table = None
airtable_orders = None
if AIRTABLE_PAT and AIRTABLE_BASE_ID:
    try:
        airtable_table = Table(AIRTABLE_PAT, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME)
        airtable_orders = Table(AIRTABLE_PAT, AIRTABLE_BASE_ID, AIRTABLE_ORDERS_TABLE)
        logger.info("Airtable connected")
    except Exception as e:
        logger.error(f"Airtable failed: {e}")

# Menu data
CATEGORIES = ["Local Dishes", "Continental", "Drinks", "Snacks"]
MENUS = {
    "Local Dishes": [("Jollof Rice", 30), ("Banku & Tilapia", 40), ("Fufu & Light Soup", 35)],
    "Continental": [("Pizza", 50), ("Burger", 25), ("Pasta", 30)],
    "Drinks": [("Coke", 5), ("Water", 2), ("Juice", 7)],
    "Snacks": [("Meat Pie", 10), ("Chips", 8), ("Samosa", 12)],
}

# In-memory session storage
memory_sessions = {}

def validate_phone_number(phone):
    """Validate phone number - must start with 233"""
    if not phone:
        return False
    
    # Remove any spaces or special characters
    clean_phone = re.sub(r'[^\d]', '', phone)
    
    # Must be 233 followed by 9 digits (233241234567)
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
            "payment_method": "",
            "network": "",
            "momo_number": "",
            "total": 0,
            "order_history": []
        }
    return memory_sessions[msisdn]

def save_session(msisdn, session):
    """Save user session to memory"""
    memory_sessions[msisdn] = session

def log_to_airtable(msisdn, userid, message, continue_session, state=None):
    """Log to Airtable"""
    if not airtable_table:
        return
    try:
        airtable_table.create({
            "MSISDN": msisdn,
            "USERID": userid,
            "Message": message,
            "ContinueSession": str(continue_session),
            "State": state or "unknown",
            "Timestamp": datetime.utcnow().isoformat()
        })
    except Exception as e:
        logger.error(f"Airtable log error: {e}")

def create_order(session, msisdn):
    """Create order record"""
    order_id = str(uuid.uuid4())[:8].upper()
    
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
    
    # Log to Airtable
    if airtable_orders:
        try:
            airtable_orders.create({
                "OrderID": order_id,
                "MSISDN": msisdn,
                "Items": json.dumps(items),
                "Total": total,
                "DeliveryLocation": session["delivery_location"],
                "PaymentMethod": session["payment_method"],
                "Status": "Pending",
                "CreatedAt": datetime.utcnow().isoformat()
            })
        except Exception as e:
            logger.error(f"Order log error: {e}")
    
    # Add to user history
    session["order_history"].append({
        "order_id": order_id,
        "total": total,
        "created_at": datetime.utcnow().isoformat()
    })
    
    return order_id

def paystack_payment(msisdn, amount, network):
    """Process Paystack payment"""
    if not PAYSTACK_SECRET_KEY:
        return {"status": False, "message": "Payment not configured"}
    
    import requests
    url = "https://api.paystack.co/charge"
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "amount": int(amount * 100),  # Convert to pesewas
        "email": f"{msisdn}@flapussd.com",
        "currency": "GHS",
        "mobile_money": {
            "phone": msisdn,
            "provider": network.lower()
        }
    }
    
    try:
        r = requests.post(url, json=data, headers=headers, timeout=15)
        return r.json()
    except Exception as e:
        logger.error(f"Payment error: {e}")
        return {"status": False, "message": str(e)}

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
        
        # Debug logging
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
        elif state == "DELIVERY":
            response = handle_delivery(input_text, session, user_id, msisdn)
        elif state == "PAYMENT_METHOD":
            response = handle_payment_method(input_text, session, user_id, msisdn)
        elif state == "MOMO_NETWORK":
            response = handle_momo_network(input_text, session, user_id, msisdn)
        elif state == "MOMO_NUMBER":
            response = handle_momo_number(input_text, session, user_id, msisdn)
        elif state == "CONFIRM":
            response = handle_confirm(input_text, session, user_id, msisdn)
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
    msg = "Welcome to FLAP Dish!\n1. Order Food\n2. My Orders\n3. Help\n0. Exit"
    
    if input_text == "" or input_text == "1":
        session["state"] = "CATEGORY"
        cat_menu = "\n".join([f"{i+1}. {cat}" for i, cat in enumerate(CATEGORIES)])
        msg = f"Select Category:\n{cat_menu}\n#. Back"
    elif input_text == "2":
        orders = session.get("order_history", [])
        if orders:
            recent = orders[-3:]
            order_lines = [f"{o['order_id']}: GHS {o['total']}" for o in recent]
            msg = "Recent Orders:\n" + "\n".join(order_lines) + "\n#. Back"
        else:
            msg = "No orders yet.\n#. Back"
    elif input_text == "3":
        msg = f"Call {HELP_PHONE} for help.\n#. Back"
    elif input_text == "0":
        msg = "Thank you for using FLAP Dish!"
        return ussd_response(user_id, msisdn, msg, False)
    elif input_text == "#":
        pass  # Stay on main menu
    else:
        msg = "Invalid option.\n" + msg
    
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
        session["state"] = "PAYMENT_METHOD"
        msg = "Select payment:\n1. Mobile Money\n2. Cash\n#. Back"
        return ussd_response(user_id, msisdn, msg, True)
    
    msg = "Enter delivery location (min 3 chars):"
    return ussd_response(user_id, msisdn, msg, True)

def handle_payment_method(input_text, session, user_id, msisdn):
    """Handle payment method"""
    if input_text == "1":
        session["payment_method"] = "Mobile Money"
        session["state"] = "MOMO_NETWORK"
        msg = "Choose Network:\n1. MTN\n2. Vodafone\n3. AirtelTigo\n#. Back"
        return ussd_response(user_id, msisdn, msg, True)
    elif input_text == "2":
        session["payment_method"] = "Cash"
        session["state"] = "CONFIRM"
        return show_confirmation(session, user_id, msisdn)
    elif input_text == "#":
        session["state"] = "DELIVERY"
        msg = "Enter delivery location:"
        return ussd_response(user_id, msisdn, msg, True)
    
    msg = "Select payment:\n1. Mobile Money\n2. Cash\n#. Back"
    return ussd_response(user_id, msisdn, msg, True)

def handle_momo_network(input_text, session, user_id, msisdn):
    """Handle mobile money network"""
    nets = {"1": "mtn", "2": "vodafone", "3": "airteltigo"}
    
    if input_text == "#":
        session["state"] = "PAYMENT_METHOD"
        msg = "Select payment:\n1. Mobile Money\n2. Cash\n#. Back"
        return ussd_response(user_id, msisdn, msg, True)
    elif input_text in nets:
        session["network"] = nets[input_text]
        session["state"] = "MOMO_NUMBER"
        msg = f"Enter MoMo number or 1 to use {msisdn}:"
        return ussd_response(user_id, msisdn, msg, True)
    
    msg = "Choose Network:\n1. MTN\n2. Vodafone\n3. AirtelTigo\n#. Back"
    return ussd_response(user_id, msisdn, msg, True)

def handle_momo_number(input_text, session, user_id, msisdn):
    """Handle mobile money number"""
    if input_text == "1":
        session["momo_number"] = msisdn
        session["state"] = "CONFIRM"
        return show_confirmation(session, user_id, msisdn)
    elif validate_phone_number(input_text):
        session["momo_number"] = input_text
        session["state"] = "CONFIRM"
        return show_confirmation(session, user_id, msisdn)
    
    msg = f"Enter MoMo number or 1 to use {msisdn}:"
    return ussd_response(user_id, msisdn, msg, True)

def handle_confirm(input_text, session, user_id, msisdn):
    """Handle order confirmation"""
    if input_text == "2":
        session["cart"] = []
        session["state"] = "MAIN_MENU"
        return handle_main_menu("", session, user_id, msisdn)
    elif input_text == "1":
        # Process order
        order_id = create_order(session, msisdn)
        
        if session["payment_method"] == "Mobile Money":
            # Process payment
            pay_resp = paystack_payment(
                session.get("momo_number", msisdn),
                session["total"],
                session["network"]
            )
            
            if pay_resp.get("status"):
                session["cart"] = []
                session["state"] = "MAIN_MENU"
                msg = f"Order #{order_id} created!\nPayment prompt sent. Thanks!"
                return ussd_response(user_id, msisdn, msg, False)
            else:
                msg = f"Payment failed: {pay_resp.get('message', 'Try again')}\n1. Retry\n2. Cancel"
                return ussd_response(user_id, msisdn, msg, True)
        else:
            # Cash payment
            session["cart"] = []
            session["state"] = "MAIN_MENU"
            msg = f"Order #{order_id} placed!\nPay cash on delivery. Thanks!"
            return ussd_response(user_id, msisdn, msg, False)
    
    return show_confirmation(session, user_id, msisdn)

def show_confirmation(session, user_id, msisdn):
    """Show order confirmation"""
    lines = [f"{qty} x {item[0]} - GHS {item[1]*qty}" for item, qty in session["cart"]]
    item_count = sum(qty for item, qty in session["cart"])
    delivery_fee = 15 + (item_count - 1) * 5 if item_count > 0 else 0
    extra_charge = 2
    items_total = sum(item[1]*qty for item, qty in session["cart"])
    total = items_total + delivery_fee + extra_charge
    session["total"] = total
    
    payment_info = "Cash" if session["payment_method"] == "Cash" else f"MoMo ({session['network'].upper()})"
    
    msg = (
        "Order Summary:\n" + "\n".join(lines) +
        f"\nDelivery: GHS {delivery_fee}" +
        f"\nService: GHS {extra_charge}" +
        f"\nPayment: {payment_info}" +
        f"\nTotal: GHS {total}\n1. Confirm\n2. Cancel"
    )
    return ussd_response(user_id, msisdn, msg, True)

def ussd_response(userid, msisdn, msg, continue_session=True):
    """Generate USSD response"""
    truncated_msg = msg[:120]  # USSD limit
    
    # Log to Airtable
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
        "timestamp": datetime.utcnow().isoformat(),
        "airtable": "connected" if airtable_table else "disabled"
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting USSD Food Ordering on port {port}")
    app.run(host="0.0.0.0", port=port)
