import os
import pyotp
import requests
from dotenv import load_dotenv
from growwapi import GrowwAPI

# 1. Load your env just like before
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, "config.env")
load_dotenv(ENV_PATH)

api_key = os.getenv('GROWW_API_KEY')
totp_secret = os.getenv('TOTP_SECRET')
totp = pyotp.TOTP(totp_secret).now()

# 2. Save the original requests.post method
original_post = requests.post


# 3. Create a wrapper to intercept and print the network traffic
def debug_post(*args, **kwargs):
    print("\n--- OUTGOING REQUEST ---")
    print(f"URL: {args[0] if args else kwargs.get('url')}")
    print(f"Headers: {kwargs.get('headers')}")
    print(f"Payload: {kwargs.get('json')}")

    # Actually send the request
    response = original_post(*args, **kwargs)

    print("\n--- INCOMING RESPONSE ---")
    print(f"Status Code: {response.status_code}")
    print(f"Raw Server Response: {response.text}\n")
    return response


# 4. Inject our wrapper
requests.post = debug_post

# 5. Try authenticating
print("Attempting to authenticate and capture raw traffic...")
try:
    GrowwAPI.get_access_token(api_key=api_key, totp=totp)
except Exception as e:
    print(f"Library raised exception: {e}")