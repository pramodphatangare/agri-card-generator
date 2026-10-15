import base64
import io
import os
import re
import shutil
import subprocess
import fitz  # PyMuPDF
import pandas as pd
from PIL import Image
import pytesseract
from pytesseract import Output
import qrcode
from playwright.sync_api import sync_playwright
import streamlit as st

# ----------------- 1. APP CONFIGURATION & CREDENTIALS -----------------

st.set_page_config(page_title="Agri Farmer Card Portal", layout="wide", page_icon="🌾")

ADMIN_USERNAME = st.secrets.get("AUTH_USER", "admin")
ADMIN_PASSWORD = st.secrets.get("AUTH_PASS", "agri@123")

LOGO_FILENAME = "logo.png"

# Session state initialization
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

# ----------------- 3. LOCAL ASSET ENGINES -----------------

def get_local_logo_base64():
    for filename in [LOGO_FILENAME, "logo.jpg", "logo.jpeg", "logo.webp"]:
        if os.path.exists(filename):
            try:
                with open(filename, "rb") as f:
                    ext = filename.split(".")[-1].lower()
                    mime = "jpeg" if ext in ["jpg", "jpeg"] else ext
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                    return f"data:image/{mime};base64,{b64}"
            except Exception:
                pass
    return None

def generate_local_qr_base64(data_text):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=1,
    )
    qr.add_data(data_text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#000000", back_color="#ffffff")
    
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode("utf-8")

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
            v_val = "अकलापूर" if "AKALAP" in raw_v.upper() else raw_v
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

# ----------------- 6. COMBINED CARD HTML WITH WATERMARK SUPPORT -----------------

def get_cards_page_html(data):
    photo_tag = f'<img src="data:image/jpeg;base64,{data["photo_base64"]}" style="width:100%; height:100%; object-fit:cover; object-position:center center; display:block;" />' if data.get("photo_base64") else '<div style="background:#eee;width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:#666;font-size:12px;">No Photo</div>'
    
    aadhaar_val = data.get("aadhaar_no", "").strip()
    if aadhaar_val:
        clean_num = aadhaar_val.replace(" ", "")
        if len(clean_num) == 12:
            aadhaar_display = f"{clean_num[:4]} {clean_num[4:8]} {clean_num[8:]}"
        else:
            aadhaar_display = aadhaar_val
    else:
        aadhaar_display = "[Aadhaar Redacted]"

    raw_fid = data.get("farmer_id", "-")
    if len(raw_fid) == 11:
        formatted_fid = f"{raw_fid[:4]} {raw_fid[4:8]} {raw_fid[8:]}"
    else:
        formatted_fid = raw_fid

    qr_payload = f"FarmerID:{raw_fid}|Name:{data.get('name_en', '')}|DOB:{data.get('dob', '')}"
    qr_base64 = generate_local_qr_base64(qr_payload)

    logo_src = get_local_logo_base64()
    if logo_src:
        header_logo_html = f'<img src="{logo_src}" style="height: 38px; width: auto; object-fit: contain; vertical-align: middle;" />'
    else:
        header_logo_html = '<div style="font-size: 20px; font-weight: 800; color: #fff;">AgriStack</div>'

    land_records = data.get("land_records", [])
    land_rows_html = ""
    for r in land_records:
        land_rows_html += f"""
        <tr>
            <td>{r.get('district') or '-'}</td>
            <td>{r.get('taluka') or '-'}</td>
            <td>{r.get('village') or '-'}</td>
            <td>{r.get('survey_no') or '-'}</td>
            <td>{r.get('khata_no') or '-'}</td>
            <td>{r.get('area') or '-'}</td>
        </tr>
        """

    # Shared inner front card content
    def generate_front_inner(is_preview=False):
        watermark_div = '<div class="watermark">Pramod</div>' if is_preview else ''
        return f"""
            {watermark_div}
            <div class="header">
                <div>{header_logo_html}</div>
                <div class="header-title">फार्मर आयडी कार्ड</div>
            </div>
            <div class="content">
                <div class="photo-box">
                    {photo_tag}
                </div>
                <div class="details">
                    <div><span class="field-label">नाव :</span> <span class="name-mr">{data.get('name_mr') or '-'}</span></div>
                    <div class="name-en">{data.get('name_en') or '-'}</div>
                    <div class="field-row"><span class="field-label">जन्म दिनांक / DOB :</span> {data.get('dob') or '-'}</div>
                    <div class="field-row"><span class="field-label">लिंग / Gender :</span> {data.get('gender') or '-'}</div>
                    <div class="field-row"><span class="field-label">मोबाइल / Mobile :</span> {data.get('mobile') or '-'}</div>
                    <div class="field-row"><span class="field-label">आधार नं / Aadhaar No :</span> {aadhaar_display}</div>
                </div>
                <div class="qr-box">
                    <img src="data:image/png;base64,{qr_base64}" />
                </div>
            </div>
            <div class="footer">
                <span class="footer-text">फार्मर आयडी</span>
                <span class="footer-id">{formatted_fid}</span>
            </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <link href="https://fonts.googleapis.com/css2?family=Great+Vibes&family=Mukta:wght@400;600;700;800&display=swap" rel="stylesheet">
        <style>
            * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: 'Mukta', sans-serif; }}
            body {{ background: transparent; padding: 10px; display: flex; flex-direction: column; gap: 24px; }}
            
            .card {{
                width: 506px; height: 319px; background: #ffffff;
                position: relative; overflow: hidden; border: 1.5px solid #1B4D3E; border-radius: 10px;
            }}
            .header {{
                background: #1B4D3E; color: white; padding: 6px 18px;
                display: flex; justify-content: space-between; align-items: center;
                height: 52px;
            }}
            .header-title {{
                font-size: 23px; font-weight: 700; display: flex; align-items: center;
            }}
            .content {{
                display: flex; padding: 14px 18px 10px 18px; gap: 14px;
                align-items: flex-start;
            }}
            .photo-box {{
                width: 110px; height: 148px; border: 1.5px solid #666;
                border-radius: 4px; overflow: hidden; flex-shrink: 0; background: #ffffff;
                display: flex; align-items: center; justify-content: center;
            }}
            .details {{
                flex-grow: 1;
                max-width: 255px;
                display: flex;
                flex-direction: column;
                justify-content: space-between;
                color: #111;
            }}
            .name-mr {{ font-size: 17.5px; font-weight: 800; color: #000; line-height: 1.25; }}
            .name-en {{ font-size: 14.5px; color: #333; font-weight: 600; margin-top: 1px; margin-bottom: 4px; padding-left: 38px; }}
            .field-row {{
                margin-top: 6px;
                font-weight: 600;
                font-size: 14.5px;
                line-height: 1.45;
                color: #111;
            }}
            .field-label {{ color: #000; font-weight: 800; font-size: 14.5px; }}
            .qr-box {{ position: absolute; right: 18px; top: 66px; width: 88px; height: 88px; }}
            .qr-box img {{ width: 100%; height: 100%; }}

            /* Cursive Watermark */
            .watermark {{
                position: absolute;
                top: 50%;
                left: 50%;
                transform: translate(-50%, -50%) rotate(-24deg);
                font-family: 'Great Vibes', cursive;
                font-size: 85px;
                color: rgba(27, 77, 62, 0.14);
                pointer-events: none;
                z-index: 15;
                white-space: nowrap;
                user-select: none;
            }}

            .footer {{
                position: absolute; bottom: 0; left: 0; right: 0;
                background: #1B4D3E; height: 56px;
                display: flex; align-items: center; justify-content: space-between;
                padding: 0 24px;
            }}
            .footer-text {{
                color: #F9A825; font-size: 25px; font-weight: 800; line-height: 1;
            }}
            .footer-id {{
                color: #ffffff; font-size: 26px; font-weight: 800; letter-spacing: 2.5px; line-height: 1;
            }}

            .card-back {{ padding: 12px 20px; border: 2px solid #1B4D3E; }}
            .sec-title {{ font-size: 14.5px; font-weight: 800; color: #1B4D3E; margin-bottom: 2px; }}
            .address-box {{ margin-bottom: 8px; }}
            .address-mr {{ font-size: 12.5px; color: #111; font-weight: 700; line-height: 1.3; }}
            .address-en {{ font-size: 11px; color: #444; font-weight: 600; line-height: 1.3; margin-top: 1px; }}
            
            .table-container {{ width: 100%; border-collapse: collapse; margin-top: 2px; }}
            .table-container th {{
                color: #1B4D3E; font-size: 11px; font-weight: 700; text-align: left;
                padding-bottom: 2px; border-bottom: 1.5px solid #bbb;
            }}
            .table-container td {{
                padding: 2.5px 0; font-size: 11px; font-weight: 600; color: #222;
                border-bottom: 0.5px solid #f0f0f0;
            }}
            .disclaimer {{
                position: absolute; bottom: 8px; left: 0; right: 0; text-align: center;
                font-size: 10.5px; color: #555; font-weight: 600;
                border-top: 1px solid #ccc; padding-top: 3px; margin: 0 20px;
            }}
        </style>
    </head>
    <body>
        <!-- 1. PREVIEW FRONT CARD (WITH WATERMARK) -->
        <div id="card-front-preview" class="card">
            {generate_front_inner(is_preview=True)}
        </div>

        <!-- 2. CLEAN FRONT CARD FOR DOWNLOAD (NO WATERMARK) -->
        <div id="card-front-clean" class="card">
            {generate_front_inner(is_preview=False)}
        </div>

        <!-- 3. BACK CARD -->
        <div id="card-back" class="card card-back">
            <div class="sec-title">संपूर्ण पत्ता / Address</div>
            <div class="address-box">
                <div class="address-mr">{data.get('address_mr') or '-'}</div>
                <div class="address-en">{data.get('address_en') or '-'}</div>
            </div>

            <div class="sec-title">जमिनीची माहिती / Land Details</div>
            <table class="table-container">
                <thead>
                    <tr>
                        <th>जिल्हा</th>
                        <th>तालुका</th>
                        <th>गाव</th>
                        <th>गट नं.</th>
                        <th>खाते नं.</th>
                        <th>क्षेत्र हे.आर</th>
                    </tr>
                </thead>
                <tbody>
                    {land_rows_html}
                </tbody>
            </table>
            <div class="disclaimer">
                This card is only for personal use not for Govt. identity
            </div>
        </div>
    </body>
    </html>
    """

# ----------------- 7. ROBUST RENDER ENGINE -----------------

def render_cards_fast(html_content):
    ensure_environment_ready()
    
    system_chromium = shutil.which("chromium") or shutil.which("chromium-browser")

    launch_kwargs = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--single-process"
        ]
    }

    if system_chromium:
        launch_kwargs["executable_path"] = system_chromium

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(**launch_kwargs)
        except Exception:
            launch_kwargs.pop("executable_path", None)
            browser = p.chromium.launch(**launch_kwargs)

        page = browser.new_page(viewport={"width": 600, "height": 1100}, device_scale_factor=2)
        page.set_content(html_content, wait_until="networkidle")
        page.evaluate("document.fonts.ready")

        # Capture preview with watermark and clean version without watermark in one pass
        front_preview_bytes = page.locator("#card-front-preview").screenshot()
        front_clean_bytes = page.locator("#card-front-clean").screenshot()
        back_bytes = page.locator("#card-back").screenshot()

        browser.close()
        return front_preview_bytes, front_clean_bytes, back_bytes

# ----------------- 8. UI ROUTING & STATE MANAGEMENT -----------------

def login_page():
    st.markdown("<h2 style='text-align: center;'>🔐 Operator Login</h2>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; color: #666;'>Agri Farmer Card Generation Portal</p>", unsafe_allow_html=True)
    
    with st.form("login_form"):
        username = st.text_input("Username", placeholder="Enter username")
        password = st.text_input("Password", type="password", placeholder="Enter password")
        submit = st.form_submit_button("Sign In")

        if submit:
            if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
                st.session_state["authenticated"] = True
                st.success("Login successful!")
                st.rerun()
            else:
                st.error("Invalid Username or Password.")

def main_dashboard():
    col_title, col_logout = st.columns([5, 1])
    with col_title:
        st.title("🌾 Agri Farmer ID Card Portal")
    with col_logout:
        if st.button("Sign Out"):
            st.session_state["authenticated"] = False
            st.session_state["extracted_data"] = None
            st.session_state["card_front_preview"] = None
            st.session_state["card_front_clean"] = None
            st.session_state["card_back"] = None
            st.session_state["last_processed_file"] = None
            st.rerun()

    # STEP 1: UPLOAD PDF
    st.markdown("### 📄 Step 1: Upload Farmer Registration PDF")
    uploaded_file = st.file_uploader("Upload AgriStack Registration PDF", type=["pdf"])

    if uploaded_file is not None:
        file_bytes = uploaded_file.getvalue()
        if st.session_state["last_processed_file"] != uploaded_file.name:
            with st.spinner("Locating farmer photo and extracting land parcels..."):
                parsed = parse_agristack_pdf(file_bytes)
                st.session_state["extracted_data"] = parsed
                st.session_state["last_processed_file"] = uploaded_file.name
                st.session_state["card_front_preview"] = None
                st.session_state["card_front_clean"] = None
                st.session_state["card_back"] = None
                st.success("Data and photo captured successfully! Review and edit details below.")

    # STEP 2: EDITABLE VERIFICATION FORM
    if st.session_state["extracted_data"] is not None:
        d = st.session_state["extracted_data"]
        st.markdown("---")
        st.markdown("### ✏️ Step 2: Verify & Edit Card Details")
        st.caption("You can modify text fields, replace the photo if needed, or edit land records below.")

        with st.form("card_edit_form"):
            col_front_form, col_back_form = st.columns(2)

            with col_front_form:
                st.markdown("#### 🪪 Front Card Fields")
                val_farmer_id = st.text_input("Farmer ID (11 Digits)", value=d.get("farmer_id", ""))
                val_name_mr = st.text_input("नाव (Marathi Name)", value=d.get("name_mr", ""))
                val_name_en = st.text_input("Name in English", value=d.get("name_en", ""))
                val_dob = st.text_input("Date of Birth (DD/MM/YYYY)", value=d.get("dob", ""))
                val_gender = st.text_input("Gender (Male / Female)", value=d.get("gender", ""))
                val_mobile = st.text_input("Mobile Number", value=d.get("mobile", ""))
                val_aadhaar = st.text_input("Aadhaar Number", value=d.get("aadhaar_no", ""), placeholder="Enter 12-digit number")

                st.markdown("##### 📷 Photo Preview")
                current_photo = d.get("photo_base64", "")
                if current_photo:
                    st.image(f"data:image/jpeg;base64,{current_photo}", width=120)

                custom_photo_file = st.file_uploader("Replace Photo (Optional)", type=["jpg", "jpeg", "png"])

            with col_back_form:
                st.markdown("#### 🏡 Back Card Fields")
                val_addr_mr = st.text_area("संपूर्ण पत्ता (Marathi Address)", value=d.get("address_mr", ""), height=80)
                val_addr_en = st.text_area("Address in English", value=d.get("address_en", ""), height=80)
                
                st.markdown("##### 🌾 जमिनीची माहिती (Multiple Land Records)")
                st.caption("Add, remove, or edit land parcel rows in the table below:")

                initial_land = d.get("land_records", [])
                if not initial_land:
                    initial_land = [{"district": "", "taluka": "", "village": "", "survey_no": "", "khata_no": "-", "area": ""}]
                df_land = pd.DataFrame(initial_land)

                edited_df_land = st.data_editor(
                    df_land,
                    num_rows="dynamic",
                    use_container_width=True,
                    column_config={
                        "district": st.column_config.TextColumn("जिल्हा (District)"),
                        "taluka": st.column_config.TextColumn("तालुका (Taluka)"),
                        "village": st.column_config.TextColumn("गाव (Village)"),
                        "survey_no": st.column_config.TextColumn("गट नं. (Survey No.)"),
                        "khata_no": st.column_config.TextColumn("खाते नं. (Khata No.)"),
                        "area": st.column_config.TextColumn("क्षेत्र (Area in Ha)")
                    }
                )

            btn_generate = st.form_submit_button("✨ Generate & Preview Card", use_container_width=True)

        if btn_generate:
            with st.spinner("Rendering ID cards..."):
                final_photo_b64 = current_photo

                if custom_photo_file is not None:
                    final_photo_b64 = base64.b64encode(custom_photo_file.getvalue()).decode("utf-8")

                updated_land_records = edited_df_land.to_dict(orient="records")

                final_data = {
                    "farmer_id": val_farmer_id or "-",
                    "name_mr": val_name_mr or "-",
                    "name_en": val_name_en or "-",
                    "dob": val_dob or "-",
                    "gender": val_gender or "-",
                    "mobile": val_mobile or "-",
                    "aadhaar_no": val_aadhaar,
                    "address_mr": val_addr_mr or "-",
                    "address_en": val_addr_en or "-",
                    "land_records": updated_land_records,
                    "photo_base64": final_photo_b64
                }

                st.session_state["extracted_data"] = final_data
                full_html = get_cards_page_html(final_data)
                front_preview_png, front_clean_png, back_png = render_cards_fast(full_html)

                st.session_state["card_front_preview"] = front_preview_png
                st.session_state["card_front_clean"] = front_clean_png
                st.session_state["card_back"] = back_png

    # STEP 3: PREVIEW & DOWNLOAD
    if st.session_state["card_front_preview"] is not None and st.session_state["card_back"] is not None:
        st.markdown("---")
        st.markdown("### 🖨️ Step 3: Preview & Download Card")
        data = st.session_state["extracted_data"]

        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Front Side (Preview with Watermark)")
            st.image(st.session_state["card_front_preview"], use_container_width=True)
            st.download_button(
                label="📥 Download Clean Front Card (No Watermark)",
                data=st.session_state["card_front_clean"],
                file_name=f"{data['farmer_id']}_front.png",
                mime="image/png",
                key="btn_download_front",
                use_container_width=True
            )

        with col2:
            st.subheader("Back Side")
            st.image(st.session_state["card_back"], use_container_width=True)
            st.download_button(
                label="📥 Download Back Card",
                data=st.session_state["card_back"],
                file_name=f"{data['farmer_id']}_back.png",
                mime="image/png",
                key="btn_download_back",
                use_container_width=True
            )

if not st.session_state["authenticated"]:
    login_page()
else:
    main_dashboard()