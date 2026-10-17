import base64
import io
import os
import re
import shutil
import subprocess
import time
import zipfile
import fitz  # PyMuPDF
import pandas as pd
from PIL import Image
import pytesseract
from pytesseract import Output
import qrcode
from playwright.sync_api import sync_playwright
import streamlit as st

# ----------------- 1. APP CONFIGURATION & CREDENTIALS -----------------

st.set_page_config(page_title="Agri Farmer Portal & Card Generator", layout="wide", page_icon="🌾")

try:
    ADMIN_USERNAME = st.secrets.get("AUTH_USER", "admin")
    ADMIN_PASSWORD = st.secrets.get("AUTH_PASS", "agri@123")
except Exception:
    ADMIN_USERNAME = "admin"
    ADMIN_PASSWORD = "agri@123"

LOGO_FILENAME = "logo.png"
LEAF_FILENAME = "leaf.png"
CHROME_PROFILE_DIR = os.path.join(os.getcwd(), "chrome_csc_profile")

# Detect if running in cloud container (Linux mount path)
IS_CLOUD_ENV = os.path.exists("/mount/src") or "STREAMLIT_SERVER_PORT" in os.environ

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False
if "extracted_data" not in st.session_state:
    st.session_state["extracted_data"] = None
if "card_front_preview" not in st.session_state:
    st.session_state["card_front_preview"] = None
if "card_front_clean" not in st.session_state:
    st.session_state["card_front_clean"] = None
if "card_back" not in st.session_state:
    st.session_state["card_back"] = None
if "last_processed_file" not in st.session_state:
    st.session_state["last_processed_file"] = None

# ----------------- 2. CONTAINER STARTUP SETUP -----------------

@st.cache_resource
def ensure_environment_ready():
    """Installs Chromium binaries on the container if missing."""
    try:
        subprocess.run(["playwright", "install", "chromium"], check=True)
    except Exception:
        pass

ensure_environment_ready()

# ----------------- 3. LOCAL ASSET LOADERS & PRINT ENGINES -----------------

def get_base64_asset(filename):
    if os.path.exists(filename):
        try:
            with open(filename, "rb") as f:
                ext = filename.split(".")[-1].lower()
                mime = "jpeg" if ext in ["jpg", "jpeg"] else ext
                return f"data:image/{mime};base64,{base64.b64encode(f.read()).decode('utf-8')}"
        except Exception:
            pass
    return None

def generate_local_qr_base64(data_text):
    qr = qrcode.QRCode(version=1, box_size=6, border=1)
    qr.add_data(data_text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#000000", back_color="#ffffff")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")

def create_a4_printable_sheet(front_bytes, back_bytes, as_pdf=True):
    a4_w, a4_h = 2480, 3508
    sheet = Image.new("RGB", (a4_w, a4_h), (255, 255, 255))
    
    card_w, card_h = 1011, 638
    img_front = Image.open(io.BytesIO(front_bytes)).convert("RGB").resize((card_w, card_h), Image.Resampling.LANCZOS)
    img_back = Image.open(io.BytesIO(back_bytes)).convert("RGB").resize((card_w, card_h), Image.Resampling.LANCZOS)
    
    gap = 80
    total_w = (card_w * 2) + gap
    start_x = (a4_w - total_w) // 2
    start_y = 250
    
    sheet.paste(img_front, (start_x, start_y))
    sheet.paste(img_back, (start_x + card_w + gap, start_y))
    
    buf = io.BytesIO()
    if as_pdf:
        sheet.save(buf, format="PDF", resolution=300.0)
    else:
        sheet.save(buf, format="PNG", dpi=(300, 300))
    return buf.getvalue()

def make_zip(f, b, fid):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{fid}_front.png", f)
        zf.writestr(f"{fid}_back.png", b)
    return buf.getvalue()

# ----------------- 4. OCR PHOTO DETECTOR ENGINE -----------------

def detect_and_crop_photo_ocr(page):
    try:
        pix = page.get_pixmap(dpi=300)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        img_w, img_h = img.size

        ocr_data = pytesseract.image_to_data(img, output_type=Output.DICT, lang="eng")
        
        target_x, target_y, target_w, target_h = None, None, None, None
        n_boxes = len(ocr_data['text'])

        for i in range(n_boxes):
            text = ocr_data['text'][i].strip().lower()
            x = ocr_data['left'][i]
            y = ocr_data['top'][i]
            w = ocr_data['width'][i]
            h = ocr_data['height'][i]

            if ("photo" in text or "graph" in text) and x > (img_w * 0.65) and y < (img_h * 0.40):
                target_x, target_y, target_w, target_h = x, y, w, h
                break

        if target_x is None:
            for i in range(n_boxes):
                text = ocr_data['text'][i].strip().lower()
                x = ocr_data['left'][i]
                y = ocr_data['top'][i]
                w = ocr_data['width'][i]
                h = ocr_data['height'][i]
                if text == "farmer" and x > (img_w * 0.70) and y < (img_h * 0.35):
                    target_x, target_y, target_w, target_h = x, y, w, h
                    break

        if target_x is not None:
            crop_top = target_y + target_h + int(img_h * 0.012)
            crop_bottom = crop_top + int(img_h * 0.135)
            center_x = target_x + (target_w // 2)
            half_box_w = int(img_w * 0.048)
            crop_left = max(0, center_x - half_box_w)
            crop_right = min(img_w, center_x + half_box_w)

            cropped = img.crop((crop_left, crop_top, crop_right, crop_bottom))
            buf = io.BytesIO()
            cropped.save(buf, format="JPEG")
            return base64.b64encode(buf.getvalue()).decode("utf-8")

        clip_rect = fitz.Rect(page.rect.width * 0.880, page.rect.height * 0.205, page.rect.width * 0.980, page.rect.height * 0.380)
        fallback_pix = page.get_pixmap(clip=clip_rect, dpi=300)
        return base64.b64encode(fallback_pix.tobytes("jpeg")).decode("utf-8")

    except Exception as e:
        st.warning(f"Photo detection note: {e}")
        return ""

# ----------------- 5. HYBRID OCR & ACCURATE PARSER -----------------

def extract_text_hybrid(doc):
    text_chunks = [page.get_text("text") for page in doc]
    full_text = "\n".join(text_chunks).strip()

    if len(full_text) < 50:
        ocr_pages = []
        for i in range(min(2, len(doc))):
            page = doc[i]
            pix = page.get_pixmap(dpi=200)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            try:
                txt = pytesseract.image_to_string(img, lang="eng+mar")
            except Exception:
                try:
                    txt = pytesseract.image_to_string(img, lang="eng")
                except Exception:
                    txt = ""
            ocr_pages.append(txt)
        full_text = "\n".join(ocr_pages)

    return full_text

def parse_agristack_pdf(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page1 = doc[0]

    full_text = extract_text_hybrid(doc)

    farmer_id = ""
    fid_match = re.search(r"Farmer ID[\s\S]{0,120}?(\d{11})", full_text, re.IGNORECASE)
    if fid_match:
        farmer_id = fid_match.group(1).strip()
    else:
        all_fids = re.findall(r"\b(\d{11})\b", full_text)
        if all_fids:
            farmer_id = all_fids[0]

    details_block = ""
    block_match = re.search(r"Farmer Details[\s\S]*?(?=Gender\b|Date of Birth\b)", full_text, re.IGNORECASE)
    if block_match:
        details_block = block_match.group(0)

    mr_tokens = re.findall(r"[\u0900-\u097F]+", details_block)
    filtered_mr = [w for w in mr_tokens if w not in ["शेतकऱ्याचे", "नाव", "स्थानिक", "भाषेत", "स्थानिकभाषेत", "तपशील"]]
    name_mr = " ".join(filtered_mr) if filtered_mr else ""

    en_tokens = re.findall(r"[A-Za-z]+", details_block)
    filtered_en = [w for w in en_tokens if w.lower() not in [
        "farmer", "details", "name", "as", "per", "aadhaar", "in", "english", "local", "language", "gender", "caste"
    ]]
    name_en = " ".join(filtered_en) if filtered_en else ""

    if not name_en or len(name_en.split()) < 2:
        sig_match = re.search(r"Farmer Name\s*\n\s*([A-Za-z\s]+)", full_text)
        if sig_match:
            name_en = " ".join(sig_match.group(1).split())

    dob_match = re.search(r"Date of Birth\s*[:]?\s*(\d{2}/\d{2}/\d{4})", full_text, re.IGNORECASE)
    dob = dob_match.group(1).strip() if dob_match else ""

    gender_match = re.search(r"Gender\s*[:]?\s*([A-Za-z]+)", full_text, re.IGNORECASE)
    gender = gender_match.group(1).strip() if gender_match else ""

    mobile_match = re.search(r"Mobile Number\s*[:]?\s*(\d{10})", full_text, re.IGNORECASE)
    mobile = mobile_match.group(1).strip() if mobile_match else ""

    address_en = ""
    addr_en_match = re.search(
        r"Address In English\s*([\s\S]*?)(?:Farmer type|Address In Local Language|Occupations)",
        full_text,
        re.IGNORECASE
    )
    if addr_en_match:
        clean_en = " ".join(addr_en_match.group(1).replace("|", " ").split())
        if clean_en:
            address_en = clean_en

    address_mr = ""
    addr_mr_match = re.search(
        r"Address In Local Language\s*([\s\S]*?)(?:Occupations|Farmer type|Land Ownership Details|Consent|$)",
        full_text,
        re.IGNORECASE
    )
    if addr_mr_match:
        clean_mr = " ".join(addr_mr_match.group(1).replace("|", " ").split())
        if clean_mr:
            address_mr = clean_mr

    land_records = []
    land_block_match = re.search(r"Land Ownership Details[\s\S]*?(?=Annexure|Consent|$)", full_text, re.IGNORECASE)
    land_text = land_block_match.group(0) if land_block_match else full_text

    chunks = re.split(r"MAHARA[\s\n]*SHTRA", land_text, flags=re.IGNORECASE)
    for c in chunks[1:]:
        cells = [p.strip() for p in c.split("|") if p.strip()]
        if len(cells) >= 4:
            raw_d = "".join(cells[0].split())
            raw_t = "".join(cells[1].split())
            raw_v = "".join(cells[2].split())
            raw_s = "".join(cells[3].split())

            d_val = "अहिल्यानगर" if "AHILYA" in raw_d.upper() else raw_d
            t_val = "संगमनेर" if "SANGAM" in raw_t.upper() else raw_t
            v_val = "अकलापूर" if "AKALAP" in raw_v.upper() else v_raw
            s_val = raw_s if raw_s.isdigit() else "-"

            a_val = "-"
            for p in cells[4:14]:
                clean_num = "".join(p.replace(",", "").split())
                area_m = re.search(r"(\d+\.\d{2,6})", clean_num)
                if area_m:
                    try:
                        a_val = f"{float(area_m.group(1)):.2f}"
                    except ValueError:
                        a_val = area_m.group(1)
                    break

            if s_val != "-" or a_val != "-":
                land_records.append({
                    "district": d_val,
                    "taluka": t_val,
                    "village": v_val,
                    "survey_no": s_val,
                    "khata_no": "-",
                    "area": a_val
                })

    if not land_records:
        land_records.append({
            "district": "",
            "taluka": "",
            "village": "",
            "survey_no": "",
            "khata_no": "-",
            "area": ""
        })

    photo_base64 = detect_and_crop_photo_ocr(page1)

    return {
        "farmer_id": farmer_id,
        "name_en": name_en,
        "name_mr": name_mr,
        "dob": dob,
        "gender": gender,
        "mobile": mobile,
        "aadhaar_no": "",
        "address_en": address_en,
        "address_mr": address_mr,
        "land_records": land_records,
        "photo_base64": photo_base64
    }

# ----------------- 6. LOCAL PORTAL BACKGROUND AUTOMATION -----------------

def fetch_farmer_data_from_portal(aadhaar_number):
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=CHROME_PROFILE_DIR,
            headless=False,
            channel="chrome",
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"]
        )
        
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://mhfr.agristack.gov.in/", wait_until="domcontentloaded", timeout=60000)

        time.sleep(2)
        login_btn = page.locator("text='Login with CSC'")
        if login_btn.is_visible(timeout=4000):
            login_btn.click()
            time.sleep(2)

        if "connect.csc.gov.in" in page.url:
            st.info("🔐 Please complete CSC login & CAPTCHA in the opened Chrome window...")
            page.wait_for_url(lambda u: "agristack.gov.in" in u and "connect.csc.gov.in" not in u, timeout=180000)

        try:
            page.wait_for_url(lambda u: "addCscFarmerRegistry" in u or ("redirect" not in u and "connect.csc" not in u), timeout=30000)
        except Exception:
            pass

        try:
            okay_btn = page.locator("button:has-text('Okay'), button:has-text('OK'), .btn:has-text('Okay')").first
            if okay_btn.is_visible(timeout=6000):
                okay_btn.click()
                time.sleep(1.5)
        except Exception:
            pass

        input_selectors = [
            "input[formcontrolname='aadhaarNumber']",
            "input[placeholder*='Aadhaar']",
            "input[name*='aadhaar']",
            "input[type='text']:visible"
        ]
        
        aadhaar_input = None
        for sel in input_selectors:
            loc = page.locator(sel).first
            try:
                loc.wait_for(state="visible", timeout=8000)
                aadhaar_input = loc
                break
            except Exception:
                continue

        if not aadhaar_input:
            aadhaar_input = page.locator("input:visible").first
            aadhaar_input.wait_for(state="visible", timeout=30000)

        aadhaar_input.click()
        time.sleep(0.5)
        aadhaar_input.fill("")
        aadhaar_input.press_sequentially(aadhaar_number, delay=60)
        page.keyboard.press("Enter")
        page.keyboard.press("Tab")

        show_details_btn = page.locator(
            "button:has-text('Show Farmer Details'), "
            "button:has-text('Show Details'), "
            "button:has-text('शेतकरी तपशील'), "
            ".p-dialog-footer button.btn-primary"
        ).first

        show_details_btn.wait_for(state="visible", timeout=60000)
        show_details_btn.click()

        page.wait_for_selector("text=Contact Details", timeout=30000)
        time.sleep(4)

        page_text = page.inner_text("body")
        fid_match = re.search(r"(\d{11})\s*[\n\r\s]*Farmer Id", page_text, re.IGNORECASE)
        if fid_match:
            farmer_id = fid_match.group(1).strip()
        else:
            all_ids = re.findall(r"\b(\d{11})\b", page_text)
            farmer_id = all_ids[0] if all_ids else ""

        js_data = page.evaluate("""() => {
            function getVal(keyword) {
                const allElements = Array.from(document.querySelectorAll('*'));
                const label = allElements.find(el => {
                    const txt = (el.innerText || '').trim().toLowerCase();
                    return txt.includes(keyword.toLowerCase()) && el.children.length === 0;
                });
                if (!label) return "";

                const container = label.closest('div');
                if (container) {
                    const inp = container.querySelector('input');
                    if (inp && inp.value) return inp.value.trim();
                }

                let next = label.nextElementSibling;
                while (next) {
                    if (next.tagName === 'INPUT' && next.value) return next.value.trim();
                    const cInp = next.querySelector('input');
                    if (cInp && cInp.value) return cInp.value.trim();
                    next = next.nextElementSibling;
                }
                return "";
            }

            return {
                nameEn: getVal("Farmer Name as per Aadhar in English") || getVal("Aadhar in English"),
                nameMr: getVal("Farmer Name in Local Language") || getVal("Local Language"),
                mobile: getVal("Mobile Number"),
                dob: getVal("Date Of Birth") || getVal("Date of Birth"),
                addrEn: getVal("Address In English"),
                addrMr: getVal("Address In Local Language")
            };
        }""")

        name_en = js_data.get("nameEn", "")
        name_mr = js_data.get("nameMr", "")
        mobile = js_data.get("mobile", "")
        dob = js_data.get("dob", "")
        gender = "Male" if "Male" in page_text else "Female"
        addr_en = js_data.get("addrEn", "")
        addr_mr = js_data.get("addrMr", "")

        photo_b64 = ""
        try:
            page.evaluate("window.scrollBy(0, 300)")
            time.sleep(1)

            photo_elem = page.locator("xpath=//*[contains(text(), 'Farmer Photograph')]/following::img[1]").first
            if photo_elem.count() == 0:
                photo_elem = page.locator("img[src*='data:image'], img.farmer-photo").first

            if photo_elem.count() > 0:
                src = photo_elem.get_attribute("src")
                if "base64," in src:
                    photo_b64 = src.split("base64,")[1]
                elif src.startswith("http"):
                    photo_bytes = page.request.get(src).body()
                    photo_b64 = base64.b64encode(photo_bytes).decode("utf-8")
        except Exception:
            pass

        land_records = []
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(2)

            rows = page.locator("table tbody tr")
            if rows.count() > 0:
                for i in range(rows.count()):
                    cols = rows.nth(i).locator("td")
                    if cols.count() >= 6:
                        d_raw = cols.nth(2).inner_text().strip()
                        t_raw = cols.nth(3).inner_text().strip()
                        v_raw = cols.nth(4).inner_text().strip()
                        s_raw = cols.nth(5).inner_text().strip()

                        area_val = "0.46"
                        for c_idx in range(6, cols.count()):
                            c_txt = cols.nth(c_idx).inner_text().strip()
                            area_match = re.search(r"(\d+\.\d{2,6})", c_txt)
                            if area_match:
                                try:
                                    area_val = f"{float(area_match.group(1)):.2f}"
                                except ValueError:
                                    area_val = area_match.group(1)
                                break

                        d_mr = "अहिल्यानगर" if "AHILYA" in d_raw.upper() else d_raw
                        t_mr = "संगमनेर" if "SANGAM" in t_raw.upper() else t_raw
                        v_mr = "अकलापूर" if "AKALAP" in v_raw.upper() else v_raw

                        if s_raw and s_raw != "*":
                            land_records.append({
                                "district": d_mr or "-",
                                "taluka": t_mr or "-",
                                "village": v_mr or "-",
                                "survey_no": s_raw,
                                "khata_no": "-",
                                "area": area_val
                            })

            if not land_records:
                body_dump = page.inner_text("body")
                land_match = re.search(
                    r"MAHARASHTRA[\t\s]+([^\t\n\r]+)[\t\s]+([^\t\n\r]+)[\t\s]+([^\t\n\r]+)[\t\s]+(\d+)[\t\s]+\*[\t\s\S]*?(\d+\.\d{2,6})",
                    body_dump,
                    re.IGNORECASE
                )
                if land_match:
                    d_raw = land_match.group(1).strip()
                    t_raw = land_match.group(2).strip()
                    v_raw = land_match.group(3).strip()
                    s_raw = land_match.group(4).strip()
                    raw_area = land_match.group(5).strip()

                    try:
                        area_val = f"{float(raw_area):.2f}"
                    except ValueError:
                        area_val = raw_area

                    d_mr = "अहिल्यानगर" if "AHILYA" in d_raw.upper() else d_raw
                    t_mr = "संगमनेर" if "SANGAM" in t_raw.upper() else t_raw
                    v_mr = "अकलापूर" if "AKALAP" in v_raw.upper() else v_raw

                    land_records.append({
                        "district": d_mr,
                        "taluka": t_mr,
                        "village": v_mr,
                        "survey_no": s_raw,
                        "khata_no": "-",
                        "area": area_val
                    })

        except Exception:
            pass

        if not land_records:
            land_records.append({"district": "-", "taluka": "-", "village": "-", "survey_no": "-", "khata_no": "-", "area": "-"})

        context.close()

        return {
            "farmer_id": farmer_id or "-",
            "name_mr": name_mr or "-",
            "name_en": name_en or "-",
            "dob": dob or "-",
            "gender": gender or "-",
            "mobile": mobile or "-",
            "aadhaar_no": aadhaar_number,
            "address_mr": addr_mr or "-",
            "address_en": addr_en or "-",
            "land_records": land_records,
            "photo_base64": photo_b64
        }

# ----------------- 7. HTML CARD GENERATOR -----------------

def get_cards_page_html(data):
    photo_tag = f'<img src="data:image/jpeg;base64,{data["photo_base64"]}" style="width:100%; height:100%; object-fit:cover; display:block;" />' if data.get("photo_base64") else '<div style="background:#eee;width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:#666;">No Photo</div>'
    
    aadhaar_val = str(data.get("aadhaar_no", "")).strip()
    clean_num = aadhaar_val.replace(" ", "")
    if len(clean_num) == 12:
        aadhaar_display = f"{clean_num[:4]} {clean_num[4:8]} {clean_num[8:]}"
    elif clean_num:
        aadhaar_display = aadhaar_val
    else:
        aadhaar_display = "-"

    raw_fid = data.get("farmer_id", "-")
    formatted_fid = f"{raw_fid[:4]} {raw_fid[4:8]} {raw_fid[8:]}" if len(raw_fid) == 11 else raw_fid
    qr_b64 = generate_local_qr_base64(f"FarmerID:{raw_fid}|Name:{data.get('name_en', '')}|DOB:{data.get('dob', '')}")

    logo_src = get_base64_asset(LOGO_FILENAME)
    leaf_src = get_base64_asset(LEAF_FILENAME)

    header_logo = f'<img src="{logo_src}" style="height:38px; vertical-align:middle;" />' if logo_src else '<div style="font-size:20px; font-weight:800; color:#fff;">AgriStack</div>'
    front_leaf = f'<div class="front-leaf-box"><img src="{leaf_src}" style="width:52px;" /></div>' if leaf_src else ''
    back_leaf = f'<div class="back-leaf-box"><img src="{leaf_src}" style="width:56px; opacity:0.85;" /></div>' if leaf_src else ''

    land_rows = "".join([f"<tr><td>{r.get('district','-')}</td><td>{r.get('taluka','-')}</td><td>{r.get('village','-')}</td><td>{r.get('survey_no','-')}</td><td>{r.get('khata_no','-')}</td><td>{r.get('area','-')}</td></tr>" for r in data.get("land_records", [])])

    def generate_front(preview=False):
        watermark = '<div class="watermark">Pramod</div>' if preview else ''
        return f"""
            {watermark}
            <div class="header"><div>{header_logo}</div><div class="header-title">फार्मर आयडी कार्ड</div></div>
            <div class="content">
                <div class="left-col"><div class="photo-box">{photo_tag}</div>{front_leaf}</div>
                <div class="details">
                    <div><span class="field-label">नाव :</span> <span class="name-mr">{data.get('name_mr','-')}</span></div>
                    <div class="name-en">{data.get('name_en','-')}</div>
                    <div class="field-row"><span class="field-label">जन्म दिनांक / DOB :</span> {data.get('dob','-')}</div>
                    <div class="field-row"><span class="field-label">लिंग / Gender :</span> {data.get('gender','-')}</div>
                    <div class="field-row"><span class="field-label">मोबाइल / Mobile :</span> {data.get('mobile','-')}</div>
                    <div class="field-row"><span class="field-label">आधार नं / Aadhaar No :</span> {aadhaar_display}</div>
                </div>
                <div class="qr-box"><img src="data:image/png;base64,{qr_b64}" /></div>
            </div>
            <div class="footer"><span class="footer-text">फार्मर आयडी</span><span class="footer-id">{formatted_fid}</span></div>
        """

    return f"""
    <!DOCTYPE html><html><head>
    <link href="https://fonts.googleapis.com/css2?family=Great+Vibes&family=Mukta:wght@400;600;700;800&display=swap" rel="stylesheet">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: 'Mukta', sans-serif; }}
        body {{ background: transparent; padding: 10px; display: flex; flex-direction: column; gap: 24px; }}
        .card {{ width: 506px; height: 319px; background: #fff; position: relative; overflow: hidden; border: 1.5px solid #1B4D3E; border-radius: 10px; }}
        .header {{ background: #1B4D3E; color: white; padding: 6px 18px; display: flex; justify-content: space-between; align-items: center; height: 52px; }}
        .header-title {{ font-size: 23px; font-weight: 700; }}
        .content {{ display: flex; padding: 12px 18px 8px 18px; gap: 14px; align-items: flex-start; }}
        .left-col {{ display: flex; flex-direction: column; align-items: center; width: 108px; flex-shrink: 0; }}
        .photo-box {{ width: 108px; height: 124px; border: 1.5px solid #666; border-radius: 4px; overflow: hidden; background: #fff; display: flex; align-items: center; justify-content: center; }}
        .front-leaf-box {{ margin-top: 6px; height: 48px; display: flex; align-items: center; justify-content: center; }}
        .details {{ flex-grow: 1; max-width: 255px; color: #111; }}
        .name-mr {{ font-size: 17px; font-weight: 800; color: #000; line-height: 1.25; }}
        .name-en {{ font-size: 14px; color: #333; font-weight: 600; margin-top: 1px; margin-bottom: 2px; padding-left: 38px; }}
        .field-row {{ margin-top: 6px; font-weight: 600; font-size: 14.5px; line-height: 1.45; }}
        .field-label {{ color: #000; font-weight: 800; }}
        .qr-box {{ position: absolute; right: 18px; top: 66px; width: 88px; height: 88px; }}
        .qr-box img {{ width: 100%; height: 100%; }}
        .watermark {{ position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%) rotate(-24deg); font-family: 'Great Vibes', cursive; font-size: 85px; color: rgba(27,77,62,0.14); pointer-events: none; z-index: 15; }}
        .footer {{ position: absolute; bottom: 0; left: 0; right: 0; background: #1B4D3E; height: 56px; display: flex; align-items: center; justify-content: space-between; padding: 0 24px; }}
        .footer-text {{ color: #F9A825; font-size: 25px; font-weight: 800; }}
        .footer-id {{ color: #ffffff; font-size: 26px; font-weight: 800; letter-spacing: 2.5px; }}
        .card-back {{ padding: 12px 20px; border: 2px solid #1B4D3E; }}
        .back-leaf-box {{ position: absolute; top: 12px; right: 20px; z-index: 1; }}
        .sec-title {{ font-size: 14.5px; font-weight: 800; color: #1B4D3E; margin-bottom: 2px; }}
        .address-box {{ margin-bottom: 8px; }}
        .address-mr {{ font-size: 12.5px; color: #111; font-weight: 700; }}
        .address-en {{ font-size: 11px; color: #444; font-weight: 600; margin-top: 1px; }}
        .table-container {{ width: 100%; border-collapse: collapse; margin-top: 2px; position: relative; z-index: 2; }}
        .table-container th {{ color: #1B4D3E; font-size: 11px; font-weight: 700; text-align: left; padding-bottom: 2px; border-bottom: 1.5px solid #bbb; }}
        .table-container td {{ padding: 2.5px 0; font-size: 11px; font-weight: 600; color: #222; border-bottom: 0.5px solid #f0f0f0; }}
        .disclaimer {{ position: absolute; bottom: 8px; left: 0; right: 0; text-align: center; font-size: 11px; color: #1B4D3E; font-weight: 700; border-top: 1px solid rgba(27,77,62,0.35); padding-top: 4px; margin: 0 20px; }}
    </style></head><body>
        <div id="card-front-preview" class="card">{generate_front(True)}</div>
        <div id="card-front-clean" class="card">{generate_front(False)}</div>
        <div id="card-back" class="card card-back">
            {back_leaf}
            <div class="sec-title">संपूर्ण पत्ता / Address</div>
            <div class="address-box"><div class="address-mr">{data.get('address_mr','-')}</div><div class="address-en">{data.get('address_en','-')}</div></div>
            <div class="sec-title">जमिनीची माहिती / Land Details</div>
            <table class="table-container"><thead><tr><th>जिल्हा</th><th>तालुका</th><th>गाव</th><th>गट नं.</th><th>खाते नं.</th><th>क्षेत्र हे.आर</th></tr></thead><tbody>{land_rows}</tbody></table>
            <div class="disclaimer">This card is only for personal use not for Govt. identity</div>
        </div>
    </body></html>
    """

# ----------------- 8. RENDER ENGINE -----------------

def render_cards_fast(html_content):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"]
        )
        page = browser.new_page(viewport={"width": 600, "height": 1100}, device_scale_factor=2)
        page.set_content(html_content, wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        front_prev = page.locator("#card-front-preview").screenshot()
        front_clean = page.locator("#card-front-clean").screenshot()
        back = page.locator("#card-back").screenshot()
        browser.close()
        return front_prev, front_clean, back

# ----------------- 9. UI FLOW (CLOUD & LOCAL MODES) -----------------

st.title("🌾 Agri Farmer ID Portal & Card Generator")

# Check if query params passed data from Chrome extension
query_params = st.query_params
if "ext_data" in query_params and st.session_state["extracted_data"] is None:
    try:
        import json, urllib.parse
        raw_json = urllib.parse.unquote(query_params["ext_data"])
        st.session_state["extracted_data"] = json.loads(raw_json)
        st.success("Data imported directly from AgriStack Portal!")
    except Exception:
        pass

if IS_CLOUD_ENV:
    st.info("☁️ **Cloud Mode:** Upload your registration PDF or enter details below to generate print-ready cards.")
    
    uploaded_file = st.file_uploader("Upload AgriStack Registration PDF", type=["pdf"])
    if uploaded_file is not None:
        file_bytes = uploaded_file.getvalue()
        if st.session_state["last_processed_file"] != uploaded_file.name:
            with st.spinner("Extracting farmer data and land parcels..."):
                parsed = parse_agristack_pdf(file_bytes)
                st.session_state["extracted_data"] = parsed
                st.session_state["last_processed_file"] = uploaded_file.name
                st.session_state["card_front_preview"] = None
                st.session_state["card_front_clean"] = None
                st.session_state["card_back"] = None

    if st.session_state["extracted_data"] is not None:
        d = st.session_state["extracted_data"]
        with st.form("card_edit_form"):
            col_f, col_b = st.columns(2)
            with col_f:
                st.markdown("#### 🪪 Front Card Details")
                v_fid = st.text_input("Farmer ID", value=d.get("farmer_id", ""))
                v_nmr = st.text_input("नाव (Marathi Name)", value=d.get("name_mr", ""))
                v_nen = st.text_input("Name in English", value=d.get("name_en", ""))
                v_dob = st.text_input("Date of Birth", value=d.get("dob", ""))
                v_gen = st.text_input("Gender", value=d.get("gender", "Male"))
                v_mob = st.text_input("Mobile Number", value=d.get("mobile", ""))
                v_adh = st.text_input("12-Digit Number", value=d.get("aadhaar_no", ""))
            with col_b:
                st.markdown("#### 🏡 Back Card Details")
                v_amr = st.text_area("संपूर्ण पत्ता (Marathi)", value=d.get("address_mr", ""), height=70)
                v_aen = st.text_area("Address (English)", value=d.get("address_en", ""), height=70)
                st.markdown("##### 🌾 Land Parcels")
                df_l = pd.DataFrame(d.get("land_records", []))
                edited_df = st.data_editor(df_l, num_rows="dynamic", use_container_width=True)

            btn_gen = st.form_submit_button("✨ Generate Cards", use_container_width=True)
            if btn_gen:
                with st.spinner("Rendering ID cards..."):
                    final_data = {
                        "farmer_id": v_fid or "-",
                        "name_mr": v_nmr or "-",
                        "name_en": v_nen or "-",
                        "dob": v_dob or "-",
                        "gender": v_gen or "-",
                        "mobile": v_mob or "-",
                        "aadhaar_no": v_adh,
                        "address_mr": v_amr or "-",
                        "address_en": v_aen or "-",
                        "land_records": edited_df.to_dict(orient="records"),
                        "photo_base64": d.get("photo_base64", "")
                    }
                    st.session_state["extracted_data"] = final_data
                    html = get_cards_page_html(final_data)
                    p_p, p_c, p_b = render_cards_fast(html)
                    st.session_state["card_front_preview"] = p_p
                    st.session_state["card_front_clean"] = p_c
                    st.session_state["card_back"] = p_b

else:
    # LOCAL MODE: Automated live browser fetch
    col_input, col_action = st.columns([3, 1])
    with col_input:
        input_aadhaar = st.text_input("Enter 12-Digit Number", placeholder="Enter number", max_chars=14)
    with col_action:
        st.write("")
        st.write("")
        btn_fetch = st.button("🚀 Fetch Details from AgriStack", use_container_width=True)

    if btn_fetch:
        clean_val = input_aadhaar.replace(" ", "")
        if not clean_val or len(clean_val) != 12:
            st.error("Please enter a valid 12-digit number.")
        else:
            with st.spinner("Connecting to AgriStack via local Chrome session..."):
                extracted = fetch_farmer_data_from_portal(clean_val)
                st.session_state["extracted_data"] = extracted
                html = get_cards_page_html(extracted)
                p_prev, p_clean, p_back = render_cards_fast(html)
                st.session_state["card_front_preview"] = p_prev
                st.session_state["card_front_clean"] = p_clean
                st.session_state["card_back"] = p_back
                st.success("Details and photo fetched successfully from AgriStack!")

# Common Step: Display cards & A4 print buttons
if st.session_state["card_front_preview"] is not None:
    d = st.session_state["extracted_data"]
    st.markdown("---")
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Front Side (Preview with Watermark)")
        st.image(st.session_state["card_front_preview"], use_container_width=True)
    with col2:
        st.subheader("Back Side")
        st.image(st.session_state["card_back"], use_container_width=True)

    st.markdown("---")
    st.markdown("#### ⚡ Single-Click Print & Download Options")

    a4_pdf = create_a4_printable_sheet(st.session_state["card_front_clean"], st.session_state["card_back"], as_pdf=True)
    a4_png = create_a4_printable_sheet(st.session_state["card_front_clean"], st.session_state["card_back"], as_pdf=False)
    cards_zip = make_zip(st.session_state["card_front_clean"], st.session_state["card_back"], d.get("farmer_id", "card"))

    r1, r2, r3 = st.columns(3)
    with r1:
        st.download_button(
            "📄 Download A4 Sheet (Print-Ready PDF)",
            data=a4_pdf,
            file_name=f"{d.get('farmer_id','card')}_A4_print.pdf",
            mime="application/pdf",
            use_container_width=True
        )
    with r2:
        st.download_button(
            "🖼️ Download A4 Sheet (High-Res PNG)",
            data=a4_png,
            file_name=f"{d.get('farmer_id','card')}_A4_sheet.png",
            mime="image/png",
            use_container_width=True
        )
    with r3:
        st.download_button(
            "📦 Download Both Cards (ZIP Archive)",
            data=cards_zip,
            file_name=f"{d.get('farmer_id','card')}_both_cards.zip",
            mime="application/zip",
            use_container_width=True
        )