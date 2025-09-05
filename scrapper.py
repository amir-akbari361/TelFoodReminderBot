import os
import requests
from bs4 import BeautifulSoup

# --- Configuration ---
# You can add more menus here in the future
MENU_CONFIGS = [
    {
        "output_file": "layouts/kharazmi_menu.html",
        "payload": {
            'personGroupId': '1',
            'restId': '20',
            'isKiosk': 'false'
        },
        "description": "Kharazmi University (Karaj) Menu"
    },
    # Example for another menu (e.g., Tehran campus)
    # {
    #     "output_file": "layouts/tehran_menu_lunch.html",
    #     "payload": {
    #         'personGroupId': 'ANOTHER_GROUP_ID', # Find this from browser
    #         'restId': 'ANOTHER_RESTAURANT_ID', # Find this from browser
    #         'isKiosk': 'false'
    #     },
    #     "description": "Tehran University Lunch Menu"
    # }
]

BASE_URL = "https://efood.khu.ac.ir"
LOGIN_URL = f"{BASE_URL}/Account/Login?ReturnUrl=%2f/"
MENU_REQUEST_URL = f"{BASE_URL}/Reserves/GetReservePage"

# IMPORTANT: Reading credentials from GitHub Secrets for security
USERNAME = os.getenv("UNIVERSITY_USERNAME")
PASSWORD = os.getenv("UNIVERSITY_PASSWORD")

def scrape_menus():
    """
    Logs into the university website using a session, then fetches and saves
    all configured menu HTML files.
    """
    if not USERNAME or not PASSWORD:
        print("Error: UNIVERSITY_USERNAME or UNIVERSITY_PASSWORD secrets are not set in GitHub.")
        return

    with requests.Session() as session:
        try:
            # Step 1: Get the login page to extract the CSRF token
            print("Fetching login page to get CSRF token...")
            login_page_response = session.get(LOGIN_URL)
            login_page_response.raise_for_status()

            soup = BeautifulSoup(login_page_response.text, 'html.parser')
            token = soup.find('input', {'name': '__RequestVerificationToken'})['value']
            print("Token extracted successfully.")

            # Step 2: Send POST request to log in
            login_payload = {
                '__RequestVerificationToken': token,
                'UserName': USERNAME,
                'Password': PASSWORD,
                'RememberMe': 'false'
            }
            
            print("Sending login request...")
            login_response = session.post(LOGIN_URL, data=login_payload)
            login_response.raise_for_status()

            if "نام کاربری یافت نشد" in login_response.text or "کلمه عبور اشتباه است" in login_response.text:
                print("Error: Login failed. Please check your username and password in GitHub Secrets.")
                return

            print("Login successful.")

            # Step 3: Iterate through menu configs and fetch each one
            for config in MENU_CONFIGS:
                print("-" * 30)
                print(f"Fetching: {config['description']}...")
                
                menu_response = session.post(MENU_REQUEST_URL, data=config['payload'])
                menu_response.raise_for_status()
                
                # Step 4: Save the HTML content to the specified file
                # Ensure the directory exists
                os.makedirs(os.path.dirname(config['output_file']), exist_ok=True)
                with open(config['output_file'], "w", encoding="utf-8") as f:
                    f.write(menu_response.text)
                print(f"Menu saved successfully to {config['output_file']}")

        except requests.exceptions.RequestException as e:
            print(f"A network error occurred: {e}")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")

if __name__ == "__main__":
    scrape_menus()
