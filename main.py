
from fastapi import FastAPI, Request, Response, Form
from fastapi.responses import PlainTextResponse
from fastapi.responses import RedirectResponse, JSONResponse
from twilio.twiml.messaging_response import MessagingResponse
import httpx
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os
from urllib.parse import urlencode
from fastapi.staticfiles import StaticFiles
from supabase import create_client, Client
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware



load_dotenv()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5500"],  # You can restrict this to ["http://127.0.0.1:5500"] if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
CLIENT_ID = os.getenv("JD_CLIENT_ID")
CLIENT_SECRET = os.getenv("JD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("JD_REDIRECT_URI")
TOKEN_URL = "https://signin.johndeere.com/oauth2/aus78tnlaysMraFhC1t7/v1/token"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def save_tokens_to_supabase(access_token, refresh_token, expires_at):
    supabase.table("tokens").upsert({
        "id": 1,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at
    }).execute()

def load_tokens_from_supabase():
    result = supabase.table("tokens").select("*").eq("id", 1).single().execute()
    if result.data:
        return result.data
    return {}

@app.get("/login")
def login():
    base_url = "https://signin.johndeere.com/oauth2/aus78tnlaysMraFhC1t7/v1/authorize"
    query_params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "ag2 org2 offline_access",
        "state": "someRandomState123"
    }
    auth_url = f"{base_url}?{urlencode(query_params)}"
    return RedirectResponse(auth_url)

@app.get("/callback")
async def callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        return JSONResponse({"error": "No code returned"}, status_code=400)

    async with httpx.AsyncClient() as client:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET
        }
        response = await client.post(TOKEN_URL, data=data, headers=headers)
        if response.status_code != 200:
            return JSONResponse({"error": "Failed to exchange code", "details": response.text}, status_code=500)

        token_data = response.json()
        expires_in = token_data["expires_in"]
        expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

        save_tokens_to_supabase(
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
            expires_at=expires_at
        )

    return RedirectResponse(url="/static/success.html")

@app.get("/tokens")
def get_tokens():
    tokens = load_tokens_from_supabase()
    return tokens or {"message": "Not authenticated yet"}

async def refresh_access_token():
    tokens = load_tokens_from_supabase()
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return {"error": "No refresh token stored"}

    async with httpx.AsyncClient() as client:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        response = await client.post(TOKEN_URL, data=data, headers=headers)
        if response.status_code != 200:
            return {"error": "Failed to refresh token", "details": response.text}

        token_data = response.json()
        expires_in = token_data["expires_in"]
        expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

        save_tokens_to_supabase(
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token", refresh_token),
            expires_at=expires_at
        )

        return {"message": "Token refreshed"}

async def get_valid_access_token():
    tokens = load_tokens_from_supabase()
    if not tokens.get("access_token"):
        return None

    expires_at = datetime.fromisoformat(tokens["expires_at"])
    now = datetime.utcnow()

    # Add a 1-minute buffer to avoid edge cases
    if now >= (expires_at - timedelta(seconds=60)):
        await refresh_access_token()
        tokens = load_tokens_from_supabase()

    return tokens.get("access_token")

@app.get("/organizations")
async def get_organizations():
    access_token = await get_valid_access_token()
    if not access_token:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    url = "https://sandboxapi.deere.com/platform/organizations"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.deere.axiom.v3+json"
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)

    print("John Deere API response status:", response.status_code)
    print("John Deere API response body:", response.text)


    if response.status_code != 200:
        return JSONResponse({"error": "Failed to fetch organizations", "details": response.text}, status_code=response.status_code)

    orgs = response.json().get("values", [])
    
    # Build message
    if not orgs:
        sms_result = send_sms("No organizations found.")
        return {"message": "No orgs", "sms": sms_result}

    names = [org.get("name", "Unnamed") for org in orgs]
    message = "John Deere Orgs:\n" + "\n".join(names[:5])  # limit to first 5 names

    sms_result = send_sms(message)
    return {"organizations": names, "sms_status": sms_result}


from twilio.rest import Client

def send_sms(message: str, to_number: str):
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_PHONE_FROM")
    always_notify = os.getenv("TWILIO_ALWAYS_NOTIFY", "")
    extra_numbers = [num.strip() for num in always_notify.split(",") if num.strip()]

    if not all([sid, token, from_number, to_number]):
        print("Missing Twilio environment variables.")
        return {"error": "Twilio config incomplete."}

    client = Client(sid, token)

    all_recipients = [to_number] + extra_numbers
    results = []

    for num in all_recipients:
        try:
            msg = client.messages.create(body=message, from_=from_number, to=num)
            results.append({"to": num, "status": "sent", "sid": msg.sid})
        except Exception as e:
            results.append({"to": num, "error": str(e)})

    return results

@app.get("/fields")
async def get_fields():
    access_token = await get_valid_access_token()
    if not access_token:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    org_id = os.getenv("ORG_ID")
    if not org_id:
        return JSONResponse({"error": "ORG_ID not set in environment"}, status_code=400)

    url = f"https://sandboxapi.deere.com/platform/organizations/{org_id}/fields"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.deere.axiom.v3+json"
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)

    print("John Deere Fields API response status:", response.status_code)
    print("John Deere Fields API response body:", response.text)

    if response.status_code != 200:
        return JSONResponse({"error": "Failed to fetch fields", "details": response.text}, status_code=response.status_code)

    fields = response.json().get("values", [])

    if not fields:
        print("No fields found.")
        return {"message": "No fields found."}

    # Limit to first 5 fields and parse
    limited_fields = fields[:10]
    parsed_fields = [
        {
            "name": field.get("name", "Unnamed"),
            "id": field.get("id"),
            "area": field.get("area", {}).get("value"),
            "area_unit": field.get("area", {}).get("unit")
        }
        for field in limited_fields
    ]

    print("Parsed Fields (first 5):")
    for f in parsed_fields:
        print(f)

    names = [f["name"] for f in parsed_fields]
    return {"fields": names, "parsed": parsed_fields}


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook_listener(request: Request):
    try:
        payload = await request.json()
        print("📩 JD Webhook Events:", payload)

        access_token = await get_valid_access_token()
        if not access_token:
            log_sms_event(number="", content="Webhook: Not authenticated", error="Not authenticated")
            return JSONResponse({"error": "Not authenticated"}, status_code=401)

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.deere.axiom.v3+json"
        }

        async with httpx.AsyncClient() as client:
            for event in payload:
                event_type = event.get("eventTypeId")
                resource_url = event.get("targetResource")

                print(f"📝 Event type: {event_type}")
                print(f"📍 Resource: {resource_url}")

                # Fetch resource data
                if resource_url:
                    resource_response = await client.get(resource_url, headers=headers)
                    print("📥 Resource GET Status:", resource_response.status_code)

                    if resource_response.status_code == 200:
                        resource_data = resource_response.json()
                        print("✅ Resource Data:", resource_data)

                        operation_data = resource_response.json()
                        message, field_name, client_name, farm_name = await format_operation_sms(operation_data, access_token)

                        if not farm_name:
                            farm_name = "Unknown Farm"
                        print(f"Farm Name: {farm_name}")

                        farm_lookup = supabase.table("sms_farms").select("client_name").eq("farm_name", farm_name).execute()
                        client_name = None
                        if farm_lookup.data and len(farm_lookup.data) == 1:
                            client_name = farm_lookup.data[0]["client_name"]
                            print(f"✅ Found client_name '{client_name}' for farm_name '{farm_name}'")
                        else:
                            print(f"⚠️ No matching farm_name '{farm_name}' in sms_farms table")

                        phone_number = None
                        if client_name:
                            phone_result = supabase.table("sms_clients").select("phone").eq("client_name", client_name).single().execute()
                            phone_number = phone_result.data.get("phone") if phone_result.data else None
                            if phone_number:
                                print(f"✅ Found phone number '{phone_number}' for client_name '{client_name}'")
                            else:
                                print(f"⚠️ No phone number found for client_name '{client_name}'")
                        # === Send SMS ===
                        if phone_number:
                            sms_result = send_sms(message, to_number=phone_number)
                            first_result = sms_result[0] if sms_result else {}
                            log_sms_event(
                                number=phone_number,
                                content=message,
                                error=None if first_result.get("status") == "sent" else first_result.get("error")
                                
                        )  
                        else:
                            error_msg = "No phone number found for farm: " + farm_name
                            sms_result = {"error": error_msg}
                            log_sms_event(number="", content=message, error=error_msg)

                            

                        print("📤 SMS Result:", sms_result)
                        print("📄 Formatted Message:", message)
                    else:
                        message = f"JD Event: {event_type}\nResource fetch failed with status {resource_response.status_code}"
                        log_sms_event(number="", content=message, error=f"Resource fetch failed with status {resource_response.status_code}")
                else:
                    message = f"JD Event: {event_type}\nNo resource URL"
                    log_sms_event(number="", content=message, error="No resource URL")

                print("📄 Message to send:", message)

        return Response(status_code=204)
    except Exception as e:
        print("Webhook error:", str(e))
        log_sms_event(number="", content="Webhook Exception", error=str(e))
        return JSONResponse({"error": str(e)}, status_code=400)




@app.post("/subscribe")
async def subscribe_to_field_ops():
    access_token = await get_valid_access_token()
    if not access_token:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    org_id = os.getenv("ORG_ID")
    if not org_id:
        return JSONResponse({"error": "ORG_ID not set in environment"}, status_code=400)
    webhook_url = "https://s-t-e-m.onrender.com/webhook"
    secret_token = "wVaS=dWgjKTyCg=g3RbDj0V51MWg+ges9SVvpgIYlbOO2aqBqtSuivWonJY31vrSzf"

    data = {
        "eventTypeId": "fieldOperation",
        "filters": [
            {
                "key": "orgId",
                "values": [org_id]
            }
        ],
        "targetEndpoint": {
            "targetType": "https",
            "uri": webhook_url
        },
        "displayName": "Field Operation Subscription (All Types)",
        "token": secret_token
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.deere.axiom.v3+json",
        "Content-Type": "application/vnd.deere.axiom.v3+json"
    }

    url = "https://sandboxapi.deere.com/platform/eventSubscriptions"

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=data, headers=headers)

    print("\n=== JD Subscription Request Debug ===")
    print("URL:", url)
    print("Request Headers:", headers)
    print("Request Body:", data)
    print("Response Status Code:", response.status_code)
    print("Response Headers:", response.headers)
    print("Response Text:", response.text)
    print("=====================================\n")

    if response.status_code != 201:
        return JSONResponse({
            "error": "Failed to subscribe",
            "status_code": response.status_code,
            "text": response.text
        }, status_code=response.status_code)

    return {"message": "Successfully subscribed to JD field operations"}

from datetime import datetime, timezone

async def format_operation_sms(operation_data, access_token):
    from zoneinfo import ZoneInfo

    def get_link(rel_type):
        links = operation_data.get("links", [])
        for link in links:
            if link.get("rel") == rel_type:
                return link.get("uri")
        return None

    field_url = get_link("field")
    client_url = get_link("client")
    farm_url = get_link("farm")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.deere.axiom.v3+json"
    }

    async def get_name(url):
        if not url:
            return "Unknown"
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 200:
                return r.json().get("name", "Unnamed")
        return "Unavailable"

    field_name, client_name, farm_name = await get_name(field_url), await get_name(client_url), await get_name(farm_url)

    products = operation_data.get("products", [])
    product_name = products[0].get("name", "Unknown Product") if products else "Unknown Product"

    end_time_str = operation_data.get("endDate")
    if end_time_str:
        # Parse ISO 8601 datetime as UTC
        dt_utc = datetime.fromisoformat(end_time_str.replace("Z", "+00:00")).astimezone(timezone.utc)
        # Convert to America/Chicago
        dt_local = dt_utc.astimezone(ZoneInfo("America/Chicago"))

        print("DEBUG UTC:", dt_utc)
        print("DEBUG LOCAL:", dt_local)

        time_formatted = dt_local.strftime("%I:%M %p").lstrip("0")
        date_formatted = dt_local.strftime("%Y-%m-%d")
        today_str = datetime.now(tz=ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
        time_suffix = "Today" if date_formatted == today_str else f"on {dt_local.strftime('%b %d')}"
    else:
        time_formatted = "Unknown Time"
        time_suffix = ""

    op_type = operation_data.get("fieldOperationType", "Operation").capitalize()
    msg = f"{op_type} of {product_name} on {field_name} was completed at {time_formatted} {time_suffix}."

    return msg, field_name, client_name, farm_name

def log_sms_event(number: str, content: str, error: str = None):
    supabase.table("sms_logs").insert({
        "number": number,
        "Content": content,
        "error": error or ""
    }).execute()

@app.post("/disabled")
def disabled_webhook():
    return Response(status_code=204)


# Load from environment or set directly
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.post("/sms-webhook", response_class=PlainTextResponse)
async def sms_webhook(
    request: Request,
    From: str = Form(...),
    Body: str = Form(...)
):
    response = MessagingResponse()
    body = Body.strip().lower()

    if body == "start":
        # Add phone number to Supabase if not already present
        existing = supabase.table("sms_clients").select("*").eq("phone_number", From).execute()
        if not existing.data:
            supabase.table("sms_clients").insert({"phone_number": From}).execute()
            response.message("✅ You've been opted in to alerts from Dirks Bros.")
        else:
            response.message("📲 You're already subscribed to Dirks Bros alerts.")
    elif body == "stop":
        # Remove phone number from Supabase
        supabase.table("sms_clients").delete().eq("phone_number", From).execute()
        response.message("🚫 You've been opted out of Dirks Bros alerts. Reply START to opt back in.")
    else:
        response.message("🤖 Reply START to subscribe or STOP to unsubscribe from Dirks Bros alerts.")

    return str(response)

@app.post("/Opt-in", response_class=PlainTextResponse)
async def handle_sms(From: str = Form(), Body: str = Form()):

    # Format: From = "+16605551234"
    try:
        phone_str = From.strip().replace("+1", "")  # Remove country code
        phone_int = int(phone_str)  # Convert to int to match Supabase int8
    except ValueError:
        return PlainTextResponse("Invalid phone number.", media_type="application/xml")

    message = Body.strip().upper()
    resp = MessagingResponse()

    if message == "START":
        # Check if phone number exists
        existing = supabase.table("sms_clients").select("*").eq("phone", phone_int).execute()
        if not existing.data:
            supabase.table("sms_clients").insert({"phone": phone_int, "opted_in": True}).execute()
            resp.message("✅ You’ve been subscribed to Dirks Bros text alerts. Text STOP to unsubscribe anytime.")
        else:
            # Update opted_in in case it's false
            supabase.table("sms_clients").update({"opted_in": True}).eq("phone", phone_int).execute()
            resp.message("📲 You’re already subscribed to Dirks Bros alerts. Text STOP to unsubscribe anytime.")
    elif message == "STOP":
        supabase.table("sms_clients").update({"opted_in": False}).eq("phone", phone_int).execute()
        resp.message("👋 You’ve been unsubscribed from Dirks Bros alerts. Reply START to re-subscribe.")
    else:
        resp.message("🤖 Unrecognized command. Text START to subscribe or STOP to unsubscribe.")

    return PlainTextResponse(content=str(resp), media_type="application/xml")