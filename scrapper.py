import os
import requests
from bs4 import BeautifulSoup

# ۱. مقادیر زیر را با اطلاعات واقعی سایت دانشگاه خود جایگزین کنید
# آدرس اصلی سایت
BASE_URL = "https://efood.khu.ac.ir"
LOGIN_URL = f"{BASE_URL}/"
MENU_REQUEST_URL = f"{BASE_URL}/Reserves/GetReservePage"

# نام کاربری و رمز عبور از GitHub Secrets خوانده می‌شود
USERNAME = os.getenv("UNIVERSITY_USERNAME")
PASSWORD = os.getenv("UNIVERSITY_PASSWORD")

# نام فایل‌هایی که می‌خواهید به‌روز شوند
# توجه: بر اساس نیاز خود، نام فایل‌ها را تغییر دهید یا موارد جدید اضافه کنید
KHARAZMI_MENU_FILE = "layouts/kharazmi_menu.html"


# TEHRAN_LUNCH_FILE = "layouts/tehran_menu_lunch.html"
# ...

def scrape_university_menu():
    """
    با استفاده از یک Session، به سایت دانشگاه لاگین کرده و فایل HTML منو را دانلود می‌کند.
    """
    with requests.Session() as session:
        try:
            # مرحله ۱: دریافت صفحه لاگین برای استخراج توکن CSRF
            print("در حال دریافت صفحه لاگین برای استخراج توکن...")
            login_page_response = session.get(LOGIN_URL)
            login_page_response.raise_for_status()

            soup = BeautifulSoup(login_page_response.text, 'html.parser')
            token = soup.find('input', {'name': '__RequestVerificationToken'})['value']
            print("توکن با موفقیت استخراج شد.")

            # مرحله ۲: ارسال درخواست POST برای لاگین
            login_payload = {
                '__RequestVerificationToken': token,
                'UserName': USERNAME,
                'Password': PASSWORD,
                'RememberMe': 'false'
            }

            print("در حال ارسال درخواست لاگین...")
            response = session.post(LOGIN_URL, data=login_payload)
            response.raise_for_status()

            if "نام کاربری یافت نشد" in response.text or "کلمه عبور اشتباه است" in response.text:
                print("خطا: نام کاربری یا رمز عبور اشتباه است.")
                return

            print("لاگین با موفقیت انجام شد.")

            # مرحله ۳: ارسال درخواست POST برای دریافت HTML منوی غذا
            # ======================= بخش مهم =======================
            # در اینجا باید payload مربوط به منوی مورد نظر خود را قرار دهید.
            # این اطلاعات را از بخش "Form Data" در مرورگر خود پیدا کنید.
            # این مقادیر مشخص می‌کنند که منوی کدام سلف و کدام هفته نمایش داده شود.
            menu_payload = {
                'personGroupId': '1',  # مثال: این عدد شناسه رستوران است
                'restId': '20',  # مثال: این عدد شناسه برنامه غذایی است
                'isKiosk': 'false'  # مثال: 0 برای هفته جاری، 1 برای هفته بعد و...
            }

            print(f"در حال دانلود منوی غذا از: {MENU_REQUEST_URL}")
            menu_response = session.post(MENU_REQUEST_URL, data=menu_payload)
            menu_response.raise_for_status()

            # مرحله ۴: ذخیره محتوای HTML در فایل
            with open(KHARAZMI_MENU_FILE, "w", encoding="utf-8") as f:
                f.write(menu_response.text)
            print(f"منوی غذا با موفقیت در فایل {KHARAZMI_MENU_FILE} ذخیره شد.")

        except requests.exceptions.RequestException as e:
            print(f"خطایی در هنگام اتصال به سایت رخ داد: {e}")
        except Exception as e:
            print(f"یک خطای غیرمنتظره رخ داد: {e}")


if __name__ == "__main__":
    scrape_university_menu()
