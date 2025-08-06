from fastapi import FastAPI, Request, Response, Form, Body
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
from pydantic import BaseModel
from twilio.rest import Client
from fastapi import APIRouter, HTTPException
import traceback
import json



load_dotenv()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5500", "https://alerts.dirksbros.com"],  # You can restrict this to ["http://127.0.0.1:5500"] if needed
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

# Only create supabase client if credentials are provided and not placeholder values
if (SUPABASE_URL and SUPABASE_KEY and
    SUPABASE_URL != "your_supabase_url" and
    SUPABASE_KEY != "your_supabase_key" and
    SUPABASE_URL.startswith("http")):
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("Supabase client initialized successfully")
else:
    supabase = None
    print("WARNING: Supabase credentials not configured. Database features will be disabled.")
    print(f"   SUPABASE_URL: {SUPABASE_URL}")
    print(f"   SUPABASE_KEY: {'[SET]' if SUPABASE_KEY else '[NOT SET]'}")

def save_tokens_to_supabase(access_token, refresh_token, expires_at):
    if not supabase:
        print("WARNING: Supabase not configured, skipping token save")
        return
    supabase.table("tokens").upsert({
        "id": 1,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at
    }).execute()

def load_tokens_from_supabase():
    if not supabase:
        print("WARNING: Supabase not configured, returning empty tokens")
        return {}
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
        log_error("send_sms", "Missing Twilio environment variables.")
        return {"error": "Twilio config incomplete."}

    client = Client(sid, token)
    all_recipients = [to_number] + extra_numbers
    results = []

    for num in all_recipients:
        try:
            msg = client.messages.create(body=message, from_=from_number, to=num)
            results.append({"to": num, "status": "sent", "sid": msg.sid})
        except Exception as e:
            log_error("send_sms", e)
            results.append({"to": num, "error": str(e)})

    return results

def send_sms_template(content_sid: str, to_number: str, content_variables: dict = None):
    """Send SMS using Twilio Content Template"""
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_PHONE_FROM")
    
    if not all([sid, token, from_number, to_number]):
        return {"error": "Twilio config incomplete."}

    client = Client(sid, token)
    try:
        # Prepare message parameters
        message_params = {
            "content_sid": content_sid,
            "from_": from_number,
            "to": to_number
        }
        
        # Add content variables if provided
        if content_variables:
            message_params["content_variables"] = json.dumps(content_variables)
            
        msg = client.messages.create(**message_params)
        return {"to": to_number, "status": "sent", "sid": msg.sid}
    except Exception as e:
        log_error("send_sms_template", e)
        return {"to": to_number, "error": str(e)}

class TemplateSMSRequest(BaseModel):
    content_sid: str
    phones: list
    content_variables: dict = None

@app.post("/send-template-sms")
async def send_template_sms(data: TemplateSMSRequest):
    """Send template SMS to multiple recipients"""
    results = []
    for phone in data.phones:
        try:
            result = send_sms_template(
                content_sid=data.content_sid,
                to_number=str(phone),
                content_variables=data.content_variables
            )
            results.append(result)
        except Exception as e:
            log_error("send-template-sms", e)
            results.append({"to": phone, "error": str(e)})
    
    print("Template SMS sent to:", data.phones)
    return {"results": results}

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


@app.get("/")
def root():
    return {
        "message": "FastAPI John Deere Integration API",
        "status": "running",
        "supabase_connected": supabase is not None,
        "endpoints": [
            "/health - Health check",
            "/login - Start John Deere OAuth",
            "/organizations - Get JD organizations",
            "/fields - Get JD fields",
            "/subscribe - Subscribe to JD webhooks"
        ]
    }

@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "supabase_connected": supabase is not None
    }


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
    if not supabase:
        print(f"WARNING: Would log SMS event: {number}, {content}, {error}")
        return
    supabase.table("sms_logs").insert({
        "number": number,
        "Content": content,
        "error": error or ""
    }).execute()

def log_error(context: str, error: Exception):
    print(f"[ERROR] {context}: {error}")
    traceback.print_exc()
    # Optionally, log to supabase or a file here

@app.post("/disabled")
def disabled_webhook():
    return Response(status_code=204)


# Supabase client already initialized above

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
            return
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

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_FROM")  # e.g., "+15556667777"

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

class SMSRequest(BaseModel):
    phone: str  # should be in 1112223333 format

@app.post("/send-confirmation-sms")
async def send_confirmation_sms(data: SMSRequest):
    to_number = f"+1{data.phone}"
    try:
        message = client.messages.create(
            body="Dirks Bros: Now offering text alerts! You'll receive automated notifications when your application or job is complete. We may also occasionally send manual updates on fertilizer markets or other valuable info. Reply STOP to opt out.",
            from_=TWILIO_PHONE_NUMBER,
            to=to_number,
            status_callback="https://s-t-e-m.onrender.com/sms-status"
        )
        return JSONResponse(content={"status": "sent", "sid": message.sid}, status_code=200)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
    
@app.post("/sms-status")
async def sms_status(request: Request):
    form = await request.form()
    message_status = form.get("MessageStatus")
    to_number = form.get("To")

    if message_status == "delivered":
        # Clean number format (remove +1 if needed)
        phone = to_number.replace("+1", "")
        supabase.table("sms_clients").upsert({
            "phone": int(phone),
            "opted_in": True
        }).execute()
    return "OK"

class BulkSMSRequest(BaseModel):
    phones: list
    message: str

@app.post("/send-bulk-sms")
async def send_bulk_sms(data: BulkSMSRequest):
    results = []
    for phone in data.phones:
        try:
            result = send_sms(data.message, to_number=str(phone))
            results.append(result)
        except Exception as e:
            log_error("send-bulk-sms", e)
            results.append({"to": phone, "error": str(e)})
    print("Bulk SMS sent to:", data.phones)
    return {"results": results}

api = APIRouter()

@api.get("/clients")
def get_clients(opted_in: bool = None):
    query = supabase.table("sms_clients").select("*")
    if opted_in is not None:
        query = query.eq("opted_in", opted_in)
    result = query.execute()
    return result.data or []

@api.post("/clients")
def create_client(client: dict):
    try:
        result = supabase.table("sms_clients").insert(client).execute()
        # Fix: Check result.data instead of result.error
        if result.data:
            return result.data[0]
        else:
            log_error("create_client", "No data returned from insert")
            raise HTTPException(status_code=400, detail="Failed to create client")
    except Exception as e:
        log_error("create_client", e)
        raise HTTPException(status_code=500, detail=str(e))

@api.put("/clients/{client_id}")
def update_client(client_id: int, update_data: dict):
    try:
        result = supabase.table("sms_clients").update(update_data).eq("id", client_id).execute()
        if result.data:
            return result.data[0]
        else:
            raise HTTPException(status_code=404, detail="Client not found")
    except Exception as e:
        log_error("update_client", e)
        raise HTTPException(status_code=500, detail=str(e))

@api.delete("/clients/{client_id}")
def delete_client(client_id: int):
    try:
        result = supabase.table("sms_clients").delete().eq("id", client_id).execute()
        if result.data:
            return {"message": "Client deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Client not found")
    except Exception as e:
        log_error("delete_client", e)
        raise HTTPException(status_code=500, detail=str(e))

# Same pattern for farms
@api.post("/farms")
def create_farm(farm: dict):
    try:
        result = supabase.table("sms_farms").insert(farm).execute()
        if result.data:
            return result.data[0]
        else:
            raise HTTPException(status_code=400, detail="Failed to create farm")
    except Exception as e:
        log_error("create_farm", e)
        raise HTTPException(status_code=500, detail=str(e))

@api.put("/farms/{farm_id}")
def update_farm(farm_id: int, update: dict):
    result = supabase.table("sms_farms").update(update).eq("id", farm_id).execute()
    if result.error:
        raise HTTPException(status_code=400, detail=result.error)
    return {"success": True}

@api.delete("/farms/{farm_id}")
def delete_farm(farm_id: int):
    result = supabase.table("sms_farms").delete().eq("id", farm_id).execute()
    if result.error:
        raise HTTPException(status_code=400, detail=result.error)
    return {"success": True}

# Mount the router
app.include_router(api, prefix="/api")

import json
import traceback

class ImageSMSRequest(BaseModel):
    phones: list
    message: str = ""  # Optional text message with image
    media_url: str     # URL of the image to send

def send_sms_with_image(message: str, to_number: str, media_url: str):
    """Send SMS with image attachment"""
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_PHONE_FROM")
    
    if not all([sid, token, from_number, to_number]):
        return {"error": "Twilio config incomplete."}

    client = Client(sid, token)
    try:
        # Prepare message parameters
        message_params = {
            "from_": from_number,
            "to": to_number,
            "media_url": [media_url]  # Twilio expects a list
        }
        
        # Add text message if provided
        if message:
            message_params["body"] = message
            
        msg = client.messages.create(**message_params)
        return {"to": to_number, "status": "sent", "sid": msg.sid}
    except Exception as e:
        log_error("send_sms_with_image", e)
        return {"to": to_number, "error": str(e)}

@app.post("/send-image-sms")
async def send_image_sms(data: ImageSMSRequest):
    """Send image SMS to multiple recipients"""
    results = []
    for phone in data.phones:
        try:
            result = send_sms_with_image(
                message=data.message,
                to_number=str(phone),
                media_url=data.media_url
            )
            results.append(result)
        except Exception as e:
            log_error("send-image-sms", e)
            results.append({"to": phone, "error": str(e)})
    
    print("Image SMS sent to:", data.phones)
    return {"results": results}

