from flask import Flask, request, jsonify
import os

app = Flask(__name__)

# In-memory session storage (for MVP only)
user_sessions = {}

CATEGORIES = ["Local Dishes", "Continental", "Drinks", "Snacks"]
MENUS = {
    "Local Dishes": [("Jollof Rice", 30), ("Banku & Tilapia", 40), ("Fufu & Light Soup", 35)],
    "Continental": [("Pizza", 50), ("Burger", 25), ("Pasta", 30)],
    "Drinks": [("Coke", 5), ("Water", 2), ("Juice", 7)],
    "Snacks": [("Meat Pie", 10), ("Chips", 8), ("Samosa", 12)],
}

PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")  # Replace with your Paystack key

def get_session(msisdn):
    if msisdn not in user_sessions:
        user_sessions[msisdn] = {
            "state": "MAIN_MENU",
            "cart": [],
            "selected_category": None,
            "selected_item": None,
            "quantity": 0,
            "delivery_location": "",
            "payment_method": "",
            "network": "",
            "email": f"{msisdn}@ussdfood.fake",
            "last_ref": None,
        }
    return user_sessions[msisdn]

@app.route("/", methods=["POST"])
@app.route("/ussd", methods=["POST"])
def ussd_handler():
    data = request.get_json()
    msisdn = data.get("MSISDN")
    input_text = data.get("USERDATA", "").strip()
    user_id = data.get("USERID", "NALOTest")

    session = get_session(msisdn)
    state = session["state"]

    # MAIN MENU
    if state == "MAIN_MENU":
        msg = (
            "Welcome to FoodExpress!\n"
            "1. Order Food\n2. My Orders\n3. Help\n0. Exit"
        )
        if input_text == "" or input_text == "1":
            session["state"] = "CATEGORY"
            # Show category menu
            cat_menu = "\n".join([f"{i+1}. {cat}" for i, cat in enumerate(CATEGORIES)])
            msg = f"Select Category:\n{cat_menu}\n#. Back"
            return ussd_response(user_id, msisdn, msg, True)
        elif input_text == "2":
            msg = "No orders yet.\n#. Back"
            return ussd_response(user_id, msisdn, msg, True)
        elif input_text == "3":
            msg = "Call 0800-FOOD for help.\n#. Back"
            return ussd_response(user_id, msisdn, msg, True)
        elif input_text == "0":
            msg = "Thank you for using FoodExpress!"
            return ussd_response(user_id, msisdn, msg, False)
        else:
            return ussd_response(user_id, msisdn, "Invalid option.\n" + msg, True)

    # CATEGORY MENU
    if state == "CATEGORY":
        cat_menu = "\n".join([f"{i+1}. {cat}" for i, cat in enumerate(CATEGORIES)])
        msg = f"Select Category:\n{cat_menu}\n#. Back"
        if input_text == "#":
            session["state"] = "MAIN_MENU"
            msg = (
                "Welcome to FoodExpress!\n"
                "1. Order Food\n2. My Orders\n3. Help\n0. Exit"
            )
            return ussd_response(user_id, msisdn, msg, True)
        elif input_text in [str(i+1) for i in range(len(CATEGORIES))]:
            cat = CATEGORIES[int(input_text)-1]
            session["selected_category"] = cat
            session["state"] = "ITEM"
            menu = MENUS[cat]
            menu_str = "\n".join([f"{i+1}. {m[0]} - GHS {m[1]}" for i, m in enumerate(menu)])
            msg = f"{cat}:\n{menu_str}\n#. Back"
            return ussd_response(user_id, msisdn, msg, True)
        else:
            return ussd_response(user_id, msisdn, msg, True)

    # ITEM MENU
    if state == "ITEM":
        cat = session["selected_category"]
        menu = MENUS[cat]
        menu_str = "\n".join([f"{i+1}. {m[0]} - GHS {m[1]}" for i, m in enumerate(menu)])
        msg = f"{cat}:\n{menu_str}\n#. Back"
        if input_text == "#":
            session["state"] = "CATEGORY"
            cat_menu = "\n".join([f"{i+1}. {cat}" for i, cat in enumerate(CATEGORIES)])
            msg = f"Select Category:\n{cat_menu}\n#. Back"
            return ussd_response(user_id, msisdn, msg, True)
        elif input_text in [str(i+1) for i in range(len(menu))]:
            item = menu[int(input_text)-1]
            session["selected_item"] = item
            session["state"] = "QTY"
            msg = f"You selected {item[0]}.\nEnter quantity:"
            return ussd_response(user_id, msisdn, msg, True)
        else:
            return ussd_response(user_id, msisdn, msg, True)

    # QUANTITY
    if state == "QTY":
        item = session["selected_item"][0]
        msg = f"You selected {item}.\nEnter quantity:"
        if input_text.isdigit() and int(input_text) > 0:
            qty = int(input_text)
            session["quantity"] = qty
            session["cart"].append((session["selected_item"], qty))
            session["state"] = "CART"
            msg = f"{qty} x {item} added to cart.\n1. Add more\n2. Checkout\n#. Cancel"
            return ussd_response(user_id, msisdn, msg, True)
        else:
            return ussd_response(user_id, msisdn, msg, True)

    # ADD MORE OR CHECKOUT
    if state == "CART":
        msg = "1. Add more\n2. Checkout\n#. Cancel"
        if input_text == "1":
            session["state"] = "CATEGORY"
            cat_menu = "\n".join([f"{i+1}. {cat}" for i, cat in enumerate(CATEGORIES)])
            msg = f"Select Category:\n{cat_menu}\n#. Back"
            return ussd_response(user_id, msisdn, msg, True)
        elif input_text == "2":
            session["state"] = "DELIVERY"
            msg = "Enter delivery location:"
            return ussd_response(user_id, msisdn, msg, True)
        elif input_text == "#":
            session["cart"] = []
            session["state"] = "MAIN_MENU"
            msg = "Order cancelled.\n1. Order Food\n2. My Orders\n3. Help\n0. Exit"
            return ussd_response(user_id, msisdn, msg, True)
        else:
            return ussd_response(user_id, msisdn, "Invalid option.\n" + msg, True)

    # DELIVERY LOCATION
    if state == "DELIVERY":
        msg = "Enter delivery location:"
        if input_text == "":
            return ussd_response(user_id, msisdn, msg, True)
        session["delivery_location"] = input_text
        session["state"] = "PAYMENT_METHOD"
        msg = "Select payment:\n1. Mobile Money\n2. Cash\n#. Back"
        return ussd_response(user_id, msisdn, msg, True)

    # PAYMENT METHOD
    if state == "PAYMENT_METHOD":
        msg = "Select payment:\n1. Mobile Money\n2. Cash\n#. Back"
        if input_text == "1":
            session["payment_method"] = "Mobile Money"
            session["state"] = "MOMO_NETWORK"
            msg = "Choose Network:\n1. MTN\n2. Vodafone\n3. AirtelTigo\n#. Back"
            return ussd_response(user_id, msisdn, msg, True)
        elif input_text == "2":
            session["payment_method"] = "Cash"
            session["state"] = "CONFIRM"
            # Show confirmation
            return confirm_order(user_id, msisdn, session)
        elif input_text == "#":
            session["state"] = "DELIVERY"
            msg = "Enter delivery location:"
            return ussd_response(user_id, msisdn, msg, True)
        else:
            return ussd_response(user_id, msisdn, msg, True)

    # MOBILE MONEY NETWORK
    if state == "MOMO_NETWORK":
        msg = "Choose Network:\n1. MTN\n2. Vodafone\n3. AirtelTigo\n#. Back"
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
        else:
            return ussd_response(user_id, msisdn, msg, True)

    # MOBILE MONEY NUMBER (can use MSISDN)
    if state == "MOMO_NUMBER":
        msg = f"Enter MoMo number or 1 to use {msisdn}:"
        if input_text == "":
            return ussd_response(user_id, msisdn, msg, True)
        if input_text == "1":
            session["momo_number"] = msisdn
        else:
            session["momo_number"] = input_text
        session["state"] = "CONFIRM"
        return confirm_order(user_id, msisdn, session)

    # CONFIRM ORDER & INITIATE PAYMENT
    if state == "CONFIRM":
        if input_text == "" or input_text not in ["1", "2"]:
            # Show confirmation menu again
            return confirm_order(user_id, msisdn, session)
        if input_text == "2":
            session["cart"] = []
            session["state"] = "MAIN_MENU"
            msg = "Order cancelled.\n1. Order Food\n2. My Orders\n3. Help\n0. Exit"
            return ussd_response(user_id, msisdn, msg, True)
        # Payment
        if session["payment_method"] == "Mobile Money":
            momo = session.get("momo_number", msisdn)
            network = session["network"]
            total = sum(item[1]*qty for item, qty in session["cart"])
            pay_resp = paystack_momo_payment(
                momo, total, network, PAYSTACK_SECRET_KEY, session["email"]
            )
            if pay_resp.get("status") == True:
                session["cart"] = []
                session["state"] = "MAIN_MENU"
                return ussd_response(
                    user_id, msisdn,
                    "Payment prompt sent. Approve on your phone. Thanks for ordering!",
                    False
                )
            else:
                failmsg = pay_resp.get("message", "Payment failed. Try again.")
                session["state"] = "CONFIRM"
                return ussd_response(user_id, msisdn, f"Failed: {failmsg}\n1. Try Again\n2. Cancel", True)
        else:
            session["cart"] = []
            session["state"] = "MAIN_MENU"
            return ussd_response(
                user_id, msisdn,
                "Order placed. Pay cash on delivery.\nThanks for ordering!",
                False
            )

    # fallback
    session["state"] = "MAIN_MENU"
    msg = (
        "Welcome to FoodExpress!\n"
        "1. Order Food\n2. My Orders\n3. Help\n0. Exit"
    )
    return ussd_response(user_id, msisdn, msg, True)

def confirm_order(user_id, msisdn, session):
    lines = [
        f"{qty} x {item[0]} - GHS {item[1]*qty}"
        for item, qty in session["cart"]
    ]
    total = sum(item[1]*qty for item, qty in session["cart"])
    session["total"] = total
    if session["payment_method"] == "Mobile Money":
        momo = session.get("momo_number", msisdn)
        payline = f"Mobile Money ({session['network'].capitalize()} - {momo if 'momo_number' in session else msisdn})"
    else:
        payline = "Cash"
    msg = (
        "Order:\n" + "\n".join(lines) +
        f"\nDelivery: {session['delivery_location']}" +
        f"\nPayment: {payline}" +
        f"\nTotal: GHS {total}\n1. Confirm & Pay\n2. Cancel"
    )
    return ussd_response(user_id, msisdn, msg, True)

def paystack_momo_payment(msisdn, amount, network, secret_key, email="customer@example.com"):
    import requests
    url = "https://api.paystack.co/charge"
    headers = {
        "Authorization": f"Bearer {secret_key}",
        "Content-Type": "application/json"
    }
    data = {
        "amount": int(amount * 100),  # Pesewas
        "email": email,
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
        return {"status": False, "message": str(e)}

def ussd_response(userid, msisdn, msg, continue_session=True):
    return jsonify({
        "USERID": userid,
        "MSISDN": msisdn,
        "MSG": msg[:120],  # Nalo limit
        "MSGTYPE": bool(continue_session)
    })

if __name__ == "__main__":
    # For Render, use PORT env variable if set, else default to 5000
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
