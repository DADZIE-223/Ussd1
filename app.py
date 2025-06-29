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
CATEGORIES = ["Local Dishes", "Continental", "Drinks", "Snacks"]
MENUS = {
    "Local Dishes": [("Jollof Rice", 30), ("Banku & Tilapia", 40), ("Fufu & Light Soup", 35)],
    "Continental": [("Pizza", 50), ("Burger", 25), ("Pasta", 30)],
    "Drinks": [("Coke", 5), ("Water", 2), ("Juice", 7)],
    "Snacks": [("Meat Pie", 10), ("Chips", 8), ("Samosa", 12)],
}

# In-memory session storage
memory_sessions = {}

def get_airtable_datetime():
    """Get datetime in Airtable-compatible format"""
    # Airtable expects ISO date format with 24-hour time
    return datetime.now().strftime("%Y-%m-%d %H:%M")

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
            "order_history": [],
            "session_id": str(uuid.uuid4())  # Add session ID
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
        # Always create a new record for each message to ensure logging
        # This is simpler and more reliable than trying to update existing records
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
        # Don't let Airtable errors break the USSD flow

def generate_unique_reference():
    """Generate unique payment reference with high randomness"""
    import random
    import hashlib
    
    # Use current time with microseconds + random data
    timestamp = str(time.time()).replace('.', '')
    random_data = ''.join(random.choices('0123456789abcdefghijklmnopqrstuvwxyz', k=12))
    unique_string = f"{timestamp}_{random_data}_{random.randint(100000, 999999)}"
    
    # Create hash to ensure uniqueness - only use alphanumeric characters
    hash_object = hashlib.md5(unique_string.encode())
    hash_hex = hash_object.hexdigest()[:10]
    
    # Ensure reference only contains allowed characters: alphanumeric, -, ., =
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
    
    # Log to Airtable - use "Processing" instead of "Pending"
    if airtable_orders:
        try:
            airtable_orders.create({
                "OrderID": order_id,
                "MSISDN": msisdn,
                "Items": json.dumps(items),
                "Total": total,
                "DeliveryLocation": session["delivery_location"],
                "PaymentMethod": session["payment_method"],
                "Status": "Processing",  # Changed from "Pending"
                "CreatedAt": get_airtable_datetime()
            })
        except Exception as e:
            logger.error(f"Order log error: {e}")
    
    # Add to user history
    session["order_history"].append({
        "order_id": order_id,
        "total": total,
        "created_at": get_airtable_datetime()
    })
    
    return order_id

def paystack_payment(msisdn, amount, network):
    """Process Paystack payment for Ghana Mobile Money - STK Push"""
    if not PAYSTACK_SECRET_KEY:
        return {"status": False, "message": "Payment not configured"}
    
    import requests
    
    # Map network names to Paystack providers for Ghana STK Push
    network_mapping = {
        "mtn": "mtn",
        "vodafone": "vod", 
        "airteltigo": "tgo"
    }
    
    provider = network_mapping.get(network.lower())
    if not provider:
        return {"status": False, "message": "Unsupported network"}
    
    # Format phone number correctly for each network
    def format_phone_for_network(phone, net):
        """Format phone number correctly for each network"""
        clean_phone = re.sub(r'[^\d]', '', phone)
        
        if net == "mtn":
            # MTN accepts 233XXXXXXXXX format
            return clean_phone if clean_phone.startswith('233') else f"233{clean_phone}"
        elif net == "vodafone":
            # Vodafone might need different formatting
            return clean_phone if clean_phone.startswith('233') else f"233{clean_phone}"
        elif net == "airteltigo":
            # AirtelTigo formatting
            return clean_phone if clean_phone.startswith('233') else f"233{clean_phone}"
        
        return clean_phone
    
    formatted_phone = format_phone_for_network(msisdn, network.lower())
    
    # Try up to 3 times with different references
    for attempt in range(3):
        reference = generate_unique_reference()
        
        headers = {
            "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json"
        }
        
        try:
            # Use the Mobile Money specific endpoint for STK Push
            charge_url = "https://api.paystack.co/charge"
            
            # Updated payload specifically for STK Push
            charge_data = {
                "amount": int(amount * 100),  # Convert to pesewas as integer
                "email": f"{formatted_phone}@flapussd.com",
                "currency": "GHS",
                "reference": reference,
                "channels": ["mobile_money"],  # Specify mobile money channel
                "mobile_money": {
                    "phone": formatted_phone,
                    "provider": provider
                },
                # Add these specific fields for STK Push
                "device_id": f"flap_{formatted_phone}",
                "metadata": {
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
                        },
                        {
                            "display_name": "Payment Type",
                            "variable_name": "payment_type",
                            "value": "stk_push"
                        }
                    ]
                }
            }
            
            logger.info(f"Attempt {attempt + 1}: STK Push request")
            logger.info(f"Phone: {formatted_phone}, Provider: {provider}, Amount: GHS {amount}")
            logger.info(f"Reference: {reference}")
            
            charge_response = requests.post(charge_url, json=charge_data, headers=headers, timeout=45)
            charge_result = charge_response.json()
            
            logger.info(f"Charge response: {charge_result}")
            
            # Check for duplicate reference error
            if (not charge_result.get("status") and 
                charge_result.get("code") == "duplicate_reference"):
                logger.warning(f"Duplicate reference on attempt {attempt + 1}, retrying...")
                time.sleep(2)
                continue
            
            # Handle response
            if charge_result.get("status"):
                data = charge_result.get("data", {})
                status = data.get("status", "")
                
                logger.info(f"Payment status: {status}")
                
                if status == "send_otp":
                    # STK Push sent successfully
                    display_text = data.get("display_text", "")
                    if not display_text:
                        display_text = f"Please check your {network.upper()} phone and approve the payment request"
                    
                    return {
                        "status": True, 
                        "message": "STK Push sent",
                        "reference": reference,
                        "display_text": display_text
                    }
                elif status == "send_pin":
                    # Some networks require PIN
                    return {
                        "status": True,
                        "message": "Enter your mobile money PIN",
                        "reference": reference,
                        "display_text": f"Enter your {network.upper()} mobile money PIN to complete payment"
                    }
                elif status == "success":
                    return {
                        "status": True,
                        "message": "Payment completed successfully", 
                        "reference": reference
                    }
                elif status == "pending":
                    return {
                        "status": True,
                        "message": "Payment is being processed",
                        "reference": reference,
                        "display_text": f"Please approve the payment request on your {network.upper()} phone"
                    }
                elif status == "failed":
                    error_msg = data.get("gateway_response", "Payment failed")
                    return {"status": False, "message": error_msg}
                else:
                    # For any other status, assume it's processing
                    return {
                        "status": True,
                        "message": "Payment initiated",
                        "reference": reference,
                        "display_text": f"Please check your {network.upper()} phone and approve the payment request"
                    }
            else:
                # Payment failed
                error_msg = charge_result.get("message", "Payment failed")
                logger.error(f"Payment failed: {charge_result}")
                
                # Check if it's a voucher code response (which we want to avoid)
                if "voucher" in error_msg.lower() or "ussd" in error_msg.lower():
                    logger.warning("Received voucher code response, retrying for STK push...")
                    if attempt < 2:
                        time.sleep(3)
                        continue
                    else:
                        return {"status": False, "message": "STK Push not available, please try again"}
                
                # Don't retry for certain errors
                if any(word in error_msg.lower() for word in ["insufficient", "invalid", "declined"]):
                    return {"status": False, "message": error_msg}
                
                # Retry for other errors
                if attempt == 2:  # Last attempt
                    return {"status": False, "message": error_msg}
                continue
                
        except requests.exceptions.Timeout:
            logger.error("Payment request timed out")
            if attempt == 2:  # Last attempt
                return {"status": False, "message": "Payment request timed out"}
            time.sleep(2)
            continue
        except Exception as e:
            logger.error(f"Payment error on attempt {attempt + 1}: {e}")
            if attempt == 2:  # Last attempt
                return {"status": False, "message": f"Payment error: {str(e)}"}
            time.sleep(2)
            continue
    
    # If all attempts failed
    return {"status": False, "message": "Payment failed after multiple attempts"}

def paystack_payment_alternative(msisdn, amount, network):
    """Alternative approach using Initialize Transaction then Mobile Money charge"""
    if not PAYSTACK_SECRET_KEY:
        return {"status": False, "message": "Payment not configured"}
    
    import requests
    
    network_mapping = {
        "mtn": "mtn",
        "vodafone": "vod", 
        "airteltigo": "tgo"
    }
    
    provider = network_mapping.get(network.lower())
    if not provider:
        return {"status": False, "message": "Unsupported network"}
    
    formatted_phone = re.sub(r'[^\d]', '', msisdn)
    if not formatted_phone.startswith('233'):
        formatted_phone = f"233{formatted_phone}"
    
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        # Step 1: Initialize transaction
        reference = generate_unique_reference()
        init_url = "https://api.paystack.co/transaction/initialize"
        init_data = {
            "amount": int(amount * 100),
            "email": f"{formatted_phone}@flapussd.com",
            "currency": "GHS",
            "reference": reference,
            "channels": ["mobile_money"],
            "metadata": {
                "phone": formatted_phone,
                "network": network.upper()
            }
        }
        
        init_response = requests.post(init_url, json=init_data, headers=headers, timeout=30)
        init_result = init_response.json()
        
        if not init_result.get("status"):
            return {"status": False, "message": init_result.get("message", "Transaction initialization failed")}
        
        # Step 2: Charge with mobile money
        charge_url = "https://api.paystack.co/charge"
        charge_data = {
            "amount": int(amount * 100),
            "email": f"{formatted_phone}@flapussd.com",
            "currency": "GHS",
            "reference": reference,
            "mobile_money": {
                "phone": formatted_phone,
                "provider": provider
            }
        }
        
        charge_response = requests.post(charge_url, json=charge_data, headers=headers, timeout=45)
        charge_result = charge_response.json()
        
        logger.info(f"Alternative payment response: {charge_result}")
        
        if charge_result.get("status"):
            data = charge_result.get("data", {})
            status = data.get("status", "")
            
            if status in ["send_otp", "send_pin", "pending"]:
                return {
                    "status": True,
                    "message": "STK Push sent",
                    "reference": reference,
                    "display_text": f"Please approve the payment request on your {network.upper()} phone"
                }
            elif status == "success":
                return {
                    "status": True,
                    "message": "Payment completed successfully",
                    "reference": reference
                }
        
        return {"status": False, "message": charge_result.get("message", "Payment failed")}
        
    except Exception as e:
        logger.error(f"Alternative payment error: {e}")
        return {"status": False, "message": f"Payment error: {str(e)}"}

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
        
        # Log the initial message
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
    
    # Handle initial USSD dial or empty input - show welcome menu
    if input_text == "" or input_text.startswith("*") or input_text == "1":
        if input_text == "1":
            session["state"] = "CATEGORY"
            cat_menu = "\n".join([f"{i+1}. {cat}" for i, cat in enumerate(CATEGORIES)])
            msg = f"Select Category:\n{cat_menu}\n#. Back"
        # For empty or USSD dial codes, just show the main menu
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
    elif input_text in ["1", "2", "3", "0"]:
        # Valid options are handled above
        pass
    else:
        # Only show "Invalid option" for actual invalid menu choices, not USSD dial codes
        if len(input_text) == 1 and input_text.isdigit():
            msg = "Invalid option.\n" + msg
        # For other inputs (like USSD codes), just show the main menu
    
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
            # Process payment - try main method first, then alternative if needed
            pay_resp = paystack_payment(
                session.get("momo_number", msisdn),
                session["total"],
                session["network"]
            )
            
            # If main method fails with voucher, try alternative
            if not pay_resp.get("status") and "voucher" in pay_resp.get("message", "").lower():
                logger.info("Main payment method returned voucher, trying alternative...")
                pay_resp = paystack_payment_alternative(
                    session.get("momo_number", msisdn),
                    session["total"],
                    session["network"]
                )
            
            if pay_resp.get("status"):
                session["cart"] = []
                session["state"] = "MAIN_MENU"
                
                # Use the display text from Paystack if available
                payment_msg = pay_resp.get("display_text", "Check your phone for payment prompt")
                msg = f"Order #{order_id} created!\n{payment_msg}\nThanks!"
                return ussd_response(user_id, msisdn, msg, False)
            else:
                error_msg = pay_resp.get('message', 'Payment failed')
                msg = f"Payment failed: {error_msg}\n1. Retry\n2. Cancel"
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
