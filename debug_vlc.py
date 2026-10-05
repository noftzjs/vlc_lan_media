import requests

# Test parameters matching your configuration
VLC_URL = "http://localhost:8080/requests/status.json"
PASSWORD = "Password123"  # Double check this matches your exact VLC Lua password box!

print("=========================================")
print("🔬 STARTING VLC API CONNECTOR DIAGNOSTIC")
print("=========================================\n")

try:
    print(f"Connecting to: {VLC_URL}")
    print(f"Using password authentication: '{PASSWORD}'")
    
    # Send request with blank username (VLC requirement) and basic auth
    response = requests.get(VLC_URL, auth=('', PASSWORD), timeout=5)
    
    print("\n🟢 SUCCESS! Connected to VLC.")
    print(f"HTTP Status Code: {response.status_code}")
    print(f"Server Software: {response.headers.get('Server', 'Unknown')}")
    print("VLC API responded with valid data stream.\n")
    
except requests.exceptions.ConnectionError as ce:
    print("\n🔴 CONNECTION ERROR DETECTED")
    print("FastAPI cannot see VLC at all. Possible root causes:")
    print("1. VLC is not running.")
    print("2. The 'Web' checkbox under Main Interfaces is not ticked.")
    print(f"Raw system details: {ce}")
    
except requests.exceptions.Timeout:
    print("\n🔴 TIMEOUT ERROR")
    print("The server exists but took too long to answer. Check if your local firewall or antivirus is sandboxing the connection.")

except Exception as e:
    print(f"\n🔴 UNKNOWN ERROR: {e}")

print("=========================================")
