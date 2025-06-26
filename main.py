from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse
import httpx
from datetime import datetime, timedelta
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
        return None  # not authenticated

    expires_at = datetime.fromisoformat(tokens["expires_at"])
    if datetime.utcnow() >= expires_at:
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
        "Authorization": "Bearer {access_token}",
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

@app.get("/field-operations")
async def get_field_operations():
    access_token = await get_valid_access_token()
    if not access_token:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    org_id = os.getenv("ORG_ID")
    field_id = os.getenv("FIELD_ID")
    if not org_id or not field_id:
        return JSONResponse({"error": "ORG_ID or FIELD_ID not set in environment"}, status_code=400)

    url = f"https://sandboxapi.deere.com/platform/organizations/{org_id}/fields/{field_id}/fieldOperations"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.deere.axiom.v3+json"
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)

    print("John Deere Field Operations API response status:", response.status_code)
    print("John Deere Field Operations API response body:", response.text)

    if response.status_code != 200:
        return JSONResponse({"error": "Failed to fetch field operations", "details": response.text}, status_code=response.status_code)

    operations = response.json().get("values", [])

    if not operations:
        print("No field operations found.")
        return {"message": "No field operations found."}

    # Print and return the first 5 operations
    limited_ops = operations[:20]
    print("Parsed Field Operations (first 5):")
    for op in limited_ops:
        print(op)

    return {"field_operations": limited_ops}

@app.post("/webhook")
async def webhook_listener(request: Request):
    try:
        payload = await request.json()
        print("📩 Webhook received:", payload)

        # Example: Get type of event
        event_type = payload.get("eventType")
        resource = payload.get("resource")

        # Optional: Trigger action based on eventType/resource
        message = f"JD Event: {event_type}\nResource: {resource}"
        sms_result = send_sms(message)

        return {"status": "received", "sms": sms_result}
    except Exception as e:
        print("Webhook error:", str(e))
        return JSONResponse({"error": str(e)}, status_code=400)

@app.post("/webhook")
async def webhook_listener(request: Request):
    try:
        payload = await request.json()
        print("📩 Webhook received:", payload)

        # Example: Get type of event
        event_type = payload.get("eventType")
        resource = payload.get("resource")

        # Optional: Trigger action based on eventType/resource
        message = f"JD Event: {event_type}\nResource: {resource}"
        sms_result = send_sms(message)

        return {"status": "received", "sms": sms_result}
    except Exception as e:
        print("Webhook error:", str(e))
        return JSONResponse({"error": str(e)}, status_code=400)
@app.post("/subscribe")
async def subscribe_to_field_ops():
    access_token = await get_valid_access_token()
    if not access_token:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    webhook_url = "https://your-render-app-name.onrender.com/webhook"  # Replace with your actual webhook
    secret_token = "Bearer your-secret-token"  # Replace with your secure auth token

    # JD v3 API expects this format
    data = {
        "eventType": "fieldOperation.completed",  # Exact event type
        "destination": {
            "url": webhook_url,
            "headers": [
                {
                    "name": "Authorization",
                    "value": secret_token
                }
            ]
        }
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

    return {"message": "Successfully subscribed to JD events"}
