
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse, JSONResponse
import httpx
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os
from urllib.parse import urlencode
from fastapi.staticfiles import StaticFiles
from supabase import create_client, Client
from dotenv import load_dotenv
load_dotenv()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

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

def send_sms(message: str):
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_PHONE_FROM")
    to_number = os.getenv("TWILIO_PHONE_TO")

    if not all([sid, token, from_number, to_number]):
        print("Missing Twilio environment variables.")
        return {"error": "Twilio config incomplete."}

    client = Client(sid, token)
    try:
        msg = client.messages.create(body=message, from_=from_number, to=to_number)
        return {"status": "sent", "sid": msg.sid}
    except Exception as e:
        return {"error": str(e)}

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

                        # Customize message here as needed:
                        operation_data = resource_response.json()  # assuming this is your GET response
                        message = await format_operation_sms(operation_data, access_token)


                        print("📄 Formatted Message:", message)
                    else:
                        message = f"JD Event: {event_type}\nResource fetch failed with status {resource_response.status_code}"
                else:
                    message = f"JD Event: {event_type}\nNo resource URL"

                # Send SMS
                sms_result = send_sms(message)
                print("📤 SMS Result:", sms_result)

        return Response(status_code=204)
    except Exception as e:
        print("Webhook error:", str(e))
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

async def format_operation_sms(operation_data, access_token):
    from_zone = ZoneInfo("UTC")
    to_zone = ZoneInfo("America/Chicago")

    # 1. Extract links
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

    # 2. Extract product
    products = operation_data.get("products", [])
    product_name = products[0].get("name", "Unknown Product") if products else "Unknown Product"

    # 3. Format time
    end_time_str = operation_data.get("endDate")
    if end_time_str:
        dt_utc = datetime.fromisoformat(end_time_str.replace("Z", "+00:00")).replace(tzinfo=from_zone)
        dt_local = dt_utc.astimezone(to_zone)
        time_formatted = dt_local.strftime("%I:%M %p").lstrip("0")
        date_formatted = dt_local.strftime("%Y-%m-%d")
        today_str = datetime.now(tz=to_zone).strftime("%Y-%m-%d")
        time_suffix = "Today" if date_formatted == today_str else f"on {dt_local.strftime('%b %d')}"
    else:
        time_formatted = "Unknown Time"
        time_suffix = ""

    # 4. Operation type
    op_type = operation_data.get("fieldOperationType", "Operation").capitalize()

    # 5. Final message
    return f"{op_type} of {product_name} on {field_name} was completed at {time_formatted} {time_suffix}."

@app.post("/disabled")
def disabled_webhook():
    return Response(status_code=204)