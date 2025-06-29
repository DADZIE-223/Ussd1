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
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
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
CATEGORIES = ["CHEF ONE", "Tovet", "Dine Inn - KT", "Founn", "Test"]
MENUS = {
    "CHEF ONE": [("Jollof Rice", 35), ("Banku & Tilapia", 40), ("Indomie", 35), ("FriedRice & Chicken", 35)],
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
            "payment_method": "",
            "network": "",
            "momo_number": "",
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

def generate_unique_reference():
    """Generate unique payment reference with high randomness"""
    import random
    import hashlib
    
    timestamp = str(time.time()).replace('.', '')
    random_data = ''.join(random.choices('0123456789abcdefghijklmnopqrstuvwxyz', k=12))
    unique_string = f"{timestamp}_{random_data}_{random.randint(100000, 999999)}"
    
    hash_object = hashlib.md5(unique_string.encode())
    hash_hex = hash_object.hexdigest()[:10]
    
    return f"flap{hash_hex}{int(time.time())}"

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
    
    if airtable_orders:
        try:
            airtable_orders.create({
                "OrderID": order_id,
                "MSISDN": msisdn,
                "Items": json.dumps(items),
                "Total": total,
                "DeliveryLocation": session["delivery_location"],
                "PaymentMethod": session["payment_method"],
                "Status": "Processing",
                "CreatedAt": get_airtable_datetime()
            })
        except Exception as e:
            logger.error(f"Order log error: {e}")
    
    session["order_history"].append({
        "order_id": order_id,
        "total": total,
        "created_at": get_airtable_datetime()
    })
    
    return order_id

def paystack_payment_corrected(msisdn, amount, network):
    """Process Paystack payment following their API specification exactly"""
    if not PAYSTACK_SECRET_KEY:
        return {"status": False, "message": "Payment not configured"}
    
    import requests
    
    # Correct provider mapping based on Paystack documentation
    network_mapping = {
        "mtn": "mtn",
        "vodafone": "vod", 
        "airteltigo": "tgo"
    }
    
    provider = network_mapping.get(network.lower())
    if not provider:
        return {"status": False, "message": "Unsupported network"}
    
    # Format phone number
    formatted_phone = re.sub(r'[^\d]', '', msisdn)
    if not formatted_phone.startswith('233'):
        formatted_phone = f"233{formatted_phone}"
    
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json"
    }
    
    for attempt in range(3):
        reference = generate_unique_reference()
        
        try:
            # Create charge following the exact API specification
            charge_url = "https://api.paystack.co/charge"
            
            # Structure the payload exactly as per API docs
            charge_data = {
                "email": f"{formatted_phone}@flapussd.com",
                "amount": str(int(amount * 100)),  # Amount as string in pesewas
                "reference": reference,
                "device_id": f"flap_{formatted_phone}_{int(time.time())}",
                "mobile_money": {
                    "phone": formatted_phone,
                    "provider": provider
                },
                "metadata": json.dumps({
                    "custom_fields": [
                        {
                            "display_name": "Mobile Number",
                            "variable_name": "mobile_number", 
                            "value": formatted_phone
                        },
                        {
                            "display_name": "Network",
                            "variable_name": "network",
                            "value": network.upper()
                        }
                    ]
                })
            }
            
            logger.info(f"Attempt {attempt + 1}: Paystack charge request")
            logger.info(f"Phone: {formatted_phone}, Provider: {provider}, Amount: GHS {amount}")
            logger.info(f"Reference: {reference}")
            logger.info(f"Payload: {json.dumps(charge_data, indent=2)}")
            
            charge_response = requests.post(charge_url, json=charge_data, headers=headers, timeout=60)
            charge_result = charge_response.json()
            
            logger.info(f"Charge response: {json.dumps(charge_result, indent=2)}")
            
            # Handle duplicate reference
            if (not charge_result.get("status") and 
                charge_result.get("code") == "duplicate_reference"):
                logger.warning(f"Duplicate reference on attempt {attempt + 1}, retrying...")
                time.sleep(2)
                continue
            
            # Process response
            if charge_result.get("status"):
                data = charge_result.get("data", {})
                status = data.get("status", "")
                
                logger.info(f"Payment status: {status}")
                
                # Handle different statuses
                if status == "send_otp":
                    # STK Push or OTP sent
                    display_text = data.get("display_text", "")
                    if not display_text:
                        display_text = f"Please check your {network.upper()} phone for payment prompt"
                    
                    return {
                        "status": True, 
                        "message": "Payment prompt sent",
                        "reference": reference,
                        "display_text": display_text,
                        "next_action": data.get("next_action", "")
                    }
                    
                elif status == "send_pin":
                    return {
                        "status": True,
                        "message": "Enter your mobile money PIN",
                        "reference": reference,
                        "display_text": f"Enter your {network.upper()} mobile money PIN"
                    }
                    
                elif status == "pending":
                    return {
                        "status": True,
                        "message": "Payment is being processed",
                        "reference": reference,
                        "display_text": f"Please approve the payment on your {network.upper()} phone"
                    }
                    
                elif status == "success":
                    return {
                        "status": True,
                        "message": "Payment completed successfully", 
                        "reference": reference
                    }
                    
                elif status == "failed":
                    error_msg = data.get("gateway_response", "Payment failed")
                    return {"status": False, "message": error_msg}
                    
                else:
                    # Unknown status - log and assume processing
                    logger.warning(f"Unknown payment status: {status}")
                    return {
                        "status": True,
                        "message": "Payment initiated",
                        "reference": reference,
                        "display_text": f"Please check your {network.upper()} phone for payment prompt"
                    }
            else:
                # Payment failed
                error_msg = charge_result.get("message", "Payment failed")
                logger.error(f"Payment failed: {error_msg}")
                
                # Check for voucher response
                if any(word in error_msg.lower() for word in ["voucher", "ussd", "dial"]):
                    logger.warning("Received voucher/USSD response instead of STK push")
                    if attempt < 2:
                        time.sleep(3)
                        continue
                    else:
                        return {"status": False, "message": "STK Push not available for this network. Please try a different payment method."}
                
                # Don't retry for certain errors
                if any(word in error_msg.lower() for word in ["insufficient", "invalid", "declined", "blocked"]):
                    return {"status": False, "message": error_msg}
                
                # Retry for other errors
                if attempt == 2:
                    return {"status": False, "message": error_msg}
                continue
                
        except requests.exceptions.Timeout:
            logger.error("Payment request timed out")
            if attempt == 2:
                return {"status": False, "message": "Payment request timed out"}
            time.sleep(3)
            continue
            
        except Exception as e:
            logger.error(f"Payment error on attempt {attempt + 1}: {e}")
            if attempt == 2:
                return {"status": False, "message": f"Payment error: {str(e)}"}
            time.sleep(3)
            continue
    
    return {"status": False, "message": "Payment failed after multiple attempts"}

def paystack_payment_ussd_method(msisdn, amount, network):
    """Alternative method using USSD instead of mobile money"""
    if not PAYSTACK_SECRET_KEY:
        return {"status": False, "message": "Payment not configured"}
    
    import requests
    
    # USSD codes for different networks in Ghana
    ussd_mapping = {
        "mtn": "mtn",
        "vodafone": "vodafone", 
        "airteltigo": "airteltigo"
    }
    
    ussd_type = ussd_mapping.get(network.lower())
    if not ussd_type:
        return {"status": False, "message": "Unsupported network"}
    
    formatted_phone = re.sub(r'[^\d]', '', msisdn)
    if not formatted_phone.startswith('233'):
        formatted_phone = f"233{formatted_phone}"
    
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        reference = generate_unique_reference()
        charge_url = "https://api.paystack.co/charge"
        
        # Use USSD instead of mobile_money
        charge_data = {
            "email": f"{formatted_phone}@flapussd.com",
            "amount": str(int(amount * 100)),
            "reference": reference,
            "ussd": {
                "type": ussd_type
            },
            "metadata": json.dumps({
                "phone": formatted_phone,
                "network": network.upper()
            })
        }
        
        logger.info(f"USSD payment request: {json.dumps(charge_data, indent=2)}")
        
        charge_response = requests.post(charge_url, json=charge_data, headers=headers, timeout=45)
        charge_result = charge_response.json()
        
        logger.info(f"USSD response: {json.dumps(charge_result, indent=2)}")
        
        if charge_result.get("status"):
            data = charge_result.get("data", {})
            status = data.get("status", "")
            
            if status in ["send_otp", "pending"]:
                ussd_code = data.get("ussd_code", "")
                display_text = data.get("display_text", "")
                
                if ussd_code:
                    return {
                        "status": True,
                        "message": "USSD code generated",
                        "reference": reference,
                        "display_text": f"Dial {ussd_code} on your {network.upper()} phone to complete payment"
                    }
                else:
                    return {
                        "status": True,
                        "message": "Payment initiated",
                        "reference": reference,
                        "display_text": display_text or f"Check your {network.upper()} phone for payment instructions"
                    }
            elif status == "success":
                return {
                    "status": True,
                    "message": "Payment completed successfully",
                    "reference": reference
                }
        
        return {"status": False, "message": charge_result.get("message", "USSD payment failed")}
        
    except Exception as e:
        logger.error(f"USSD payment error: {e}")
        return {"status": False, "message": f"USSD payment error: {str(e)}"}

# Use the corrected payment function as the main one
def paystack_payment(msisdn, amount, network):
    """Main payment function - tries corrected method first, then USSD fallback"""
    
    # Try the corrected mobile money method first
    result = paystack_payment_corrected(msisdn, amount, network)
    
    # If it fails with voucher/USSD response, try the USSD method
    if (not result.get("status") and 
        any(word in result.get("message", "").lower() for word in ["voucher", "ussd", "stk push not available"])):
        
        logger.info("Mobile money failed, trying USSD method...")
        result = paystack_payment_ussd_method(msisdn, amount, network)
    
    return result

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
    
    if input_text == "" or input_text.startswith("*") or input_text == "1":
        if input_text == "1":
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
        pass
    elif input_text in ["1", "2", "3", "0"]:
        pass
    else:
        if len(input_text) == 1 and input_text.isdigit():
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
        order_id = create_order(session, msisdn)
        
        if session["payment_method"] == "Mobile Money":
            pay_resp = paystack_payment(
                session.get("momo_number", msisdn),
                session["total"],
                session["network"]
            )
            
            if pay_resp.get("status"):
                session["cart"] = []
                session["state"] = "MAIN_MENU"
                
                payment_msg = pay_resp.get("display_text", "Check your phone for payment prompt")
                msg = f"Order #{order_id} created!\n{payment_msg}\nThanks!"
                return ussd_response(user_id, msisdn, msg, False)
            else:
                error_msg = pay_resp.get('message', 'Payment failed')
                msg = f"Payment failed: {error_msg}\n1. Retry\n2. Cancel"
                return ussd_response(user_id, msisdn, msg, True)
        else:
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
    truncated_msg = msg[:120]
    
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

@app.route("/test-payment", methods=["POST"])
def test_payment():
    """Test payment endpoint for debugging"""
    data = request.get_json()
    phone = data.get("phone", "233241234567")
    amount = data.get("amount", 10)
    network = data.get("network", "mtn")
    
    result = paystack_payment(phone, amount, network)
    return jsonify(result)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting USSD Food Ordering on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)
