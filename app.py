import base64
import io
import os
import re
import shutil
import subprocess
import fitz  # PyMuPDF
from PIL import Image
import pytesseract
import qrcode
from playwright.sync_api import sync_playwright
import streamlit as st

# ----------------- 1. APP CONFIGURATION & CREDENTIALS -----------------

st.set_page_config(page_title="Agri Farmer Card Portal", layout="centered", page_icon="🌾")

ADMIN_USERNAME = st.secrets.get("AUTH_USER", "admin")
ADMIN_PASSWORD = st.secrets.get("AUTH_PASS", "agri@123")

LOGO_FILENAME = "logo.png"

# Session state initialization
if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

if "card_front" not in st.session_state:
    st.session_state["card_front"] = None

if "card_back" not in st.session_state:
    st.session_state["card_back"] = None

if "farmer_data" not in st.session_state:
    st.session_state["farmer_data"] = None

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

# ----------------- 4. HYBRID OCR & PDF EXTRACTION ENGINE -----------------

def extract_text_hybrid(doc):
    """Extracts digital text or runs OCR on scanned pages."""
    text_chunks = [page.get_text("text") for page in doc]
    full_text = "\n".join(text_chunks).strip()

    # If digital text layer is empty or scanned image
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

    # 1. Farmer ID (11-digit pattern)
    farmer_id = "-"
    fid_match = re.search(r"Farmer ID[\s\S]{0,120}?(\d{11})", full_text, re.IGNORECASE)
    if fid_match:
        farmer_id = fid_match.group(1).strip()
    else:
        all_fids = re.findall(r"\b(\d{11})\b", full_text)
        if all_fids:
            farmer_id = all_fids[0]

    # 2. English Name
    name_en = "-"
    name_en_match = re.search(
        r"Farmer Name as per Aadhaar in English\s*([\s\S]*?)(?:Farmer[’']?s?\s*Name in Local Language|Gender|\n\s*\|)",
        full_text,
        re.IGNORECASE
    )
    if name_en_match:
        tokens = [w for w in name_en_match.group(1).replace("|", " ").split() if w.strip()]
        if tokens:
            name_en = " ".join(tokens)

    # 3. Marathi / Local Name
    name_mr = "-"
    name_mr_match = re.search(
        r"Name in Local Language\s*([\s\S]*?)(?:Gender|Date of Birth|Identifier|Caste Category|\n\s*\|)",
        full_text,
        re.IGNORECASE
    )
    if name_mr_match:
        tokens = [w for w in name_mr_match.group(1).replace("|", " ").split() if w.strip()]
        if tokens:
            name_mr = " ".join(tokens)

    # 4. DOB, Gender, Mobile
    dob_match = re.search(r"Date of Birth\s*[:]?\s*(\d{2}/\d{2}/\d{4})", full_text, re.IGNORECASE)
    dob = dob_match.group(1).strip() if dob_match else "-"

    gender_match = re.search(r"Gender\s*[:]?\s*([A-Za-z]+)", full_text, re.IGNORECASE)
    gender = gender_match.group(1).strip() if gender_match else "-"

    mobile_match = re.search(r"Mobile Number\s*[:]?\s*(\d{10})", full_text, re.IGNORECASE)
    mobile = mobile_match.group(1).strip() if mobile_match else "-"

    # 5. Addresses
    address_en = "-"
    addr_en_match = re.search(
        r"Address In English\s*([\s\S]*?)(?:Farmer type|Address In Local Language|Occupations)",
        full_text,
        re.IGNORECASE
    )
    if addr_en_match:
        clean_en = " ".join(addr_en_match.group(1).replace("|", " ").split())
        if clean_en:
            address_en = clean_en

    address_mr = "-"
    addr_mr_match = re.search(
        r"Address In Local Language\s*([\s\S]*?)(?:Occupations|Farmer type|Land Ownership Details|Consent|$)",
        full_text,
        re.IGNORECASE
    )
    if addr_mr_match:
        clean_mr = " ".join(addr_mr_match.group(1).replace("|", " ").split())
        if clean_mr:
            address_mr = clean_mr

    # 6. Land Details Parsing
    district_name = "-"
    taluka_name = "-"
    village_name = "-"
    survey_no = "-"
    area = "-"

    mahara_pos = full_text.upper().find("MAHARA")
    if mahara_pos != -1:
        sub = full_text[mahara_pos:mahara_pos + 1800]
        parts = [p.strip() for p in sub.split("|") if p.strip()]

        if len(parts) >= 5:
            raw_d = "".join(parts[1].split())
            raw_t = "".join(parts[2].split())
            raw_v = "".join(parts[3].split())
            raw_s = "".join(parts[4].split())

            district_name = "अहिल्यानगर" if "AHILYA" in raw_d.upper() else (raw_d or "-")
            taluka_name = "संगमनेर" if "SANGAM" in raw_t.upper() else (raw_t or "-")
            village_name = "अकलापूर" if "AKALAP" in raw_v.upper() else (raw_v or "-")
            survey_no = raw_s or "-"

            for p in parts[5:14]:
                clean_num = "".join(p.replace(",", "").split())
                area_match = re.search(r"(\d+\.\d+)", clean_num)
                if area_match:
                    try:
                        area = f"{float(area_match.group(1)):.2f}"
                    except ValueError:
                        area = area_match.group(1)
                    break

    # 7. Photo Extraction
    photo_base64 = ""
    try:
        search_hits = page1.search_for("Farmer Photograph")
        if search_hits:
            h = search_hits[0]
            clip_rect = fitz.Rect(
                h.x0 + 66,
                h.y1 + 32,
                h.x0 + 132,
                h.y1 + 104
            )
            pix = page1.get_pixmap(clip=clip_rect, dpi=300)
            photo_base64 = base64.b64encode(pix.tobytes("jpeg")).decode("utf-8")
        else:
            w = page1.rect.width
            h = page1.rect.height
            clip_rect = fitz.Rect(w * 0.89, h * 0.20, w * 0.97, h * 0.32)
            pix = page1.get_pixmap(clip=clip_rect, dpi=300)
            photo_base64 = base64.b64encode(pix.tobytes("jpeg")).decode("utf-8")
    except Exception as e:
        st.warning(f"Photo extraction notice: {e}")

    qr_payload = f"FarmerID:{farmer_id}|Name:{name_en}|DOB:{dob}"
    qr_base64 = generate_local_qr_base64(qr_payload)

    return {
        "farmer_id": farmer_id,
        "name_en": name_en,
        "name_mr": name_mr,
        "dob": dob,
        "gender": gender,
        "mobile": mobile,
        "address_en": address_en,
        "address_mr": address_mr,
        "district": district_name,
        "taluka": taluka_name,
        "village": village_name,
        "survey_no": survey_no,
        "area": area,
        "photo_base64": photo_base64,
        "qr_base64": qr_base64
    }

# ----------------- 5. COMBINED CARD HTML -----------------

def get_cards_page_html(data):
    photo_tag = f'<img src="data:image/jpeg;base64,{data["photo_base64"]}" style="width:100%; height:100%; object-fit:cover; object-position:center center; display:block;" />' if data["photo_base64"] else '<div style="background:#eee;width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:#666;font-size:12px;">No Photo</div>'
    
    aadhaar_val = data.get("aadhaar_no", "").strip()
    if aadhaar_val:
        clean_num = aadhaar_val.replace(" ", "")
        if len(clean_num) == 12:
            aadhaar_display = f"{clean_num[:4]} {clean_num[4:8]} {clean_num[8:]}"
        else:
            aadhaar_display = aadhaar_val
    else:
        aadhaar_display = "-"

    raw_fid = data['farmer_id']
    if len(raw_fid) == 11:
        formatted_fid = f"{raw_fid[:4]} {raw_fid[4:8]} {raw_fid[8:]}"
    else:
        formatted_fid = raw_fid

    logo_src = get_local_logo_base64()
    if logo_src:
        header_logo_html = f'<img src="{logo_src}" style="height: 38px; width: auto; object-fit: contain; vertical-align: middle;" />'
    else:
        header_logo_html = '<div style="font-size: 20px; font-weight: 800; color: #fff;">AgriStack</div>'

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <link href="https://fonts.googleapis.com/css2?family=Mukta:wght@400;600;700;800&display=swap" rel="stylesheet">
        <style>
            * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: 'Mukta', sans-serif; }}
            body {{ background: transparent; padding: 10px; display: flex; flex-direction: column; gap: 20px; }}
            
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
            .content {{ display: flex; padding: 12px 18px; gap: 14px; }}
            .photo-box {{
                width: 105px; height: 130px; border: 1.5px solid #666;
                border-radius: 4px; overflow: hidden; flex-shrink: 0; background: #ffffff;
                display: flex; align-items: center; justify-content: center;
            }}
            .details {{ flex-grow: 1; font-size: 13.5px; line-height: 1.35; color: #111; }}
            .name-mr {{ font-size: 16px; font-weight: 700; color: #000; }}
            .name-en {{ font-size: 13.5px; color: #333; font-weight: 600; margin-bottom: 2px; }}
            .field-row {{ margin-top: 2px; font-weight: 600; font-size: 13px; }}
            .field-label {{ color: #111; font-weight: 700; }}
            .qr-box {{ position: absolute; right: 18px; top: 68px; width: 85px; height: 85px; }}
            .qr-box img {{ width: 100%; height: 100%; }}

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

            .card-back {{ padding: 14px 22px; border: 2px solid #1B4D3E; }}
            .sec-title {{ font-size: 15px; font-weight: 800; color: #1B4D3E; margin-bottom: 2px; }}
            .address-box {{ margin-bottom: 12px; }}
            .address-mr {{ font-size: 13px; color: #111; font-weight: 700; line-height: 1.25; }}
            .address-en {{ font-size: 11.5px; color: #444; font-weight: 600; line-height: 1.25; margin-top: 2px; }}
            .table-container {{ width: 100%; border-collapse: collapse; margin-top: 4px; }}
            .table-container th {{
                color: #1B4D3E; font-size: 12.5px; font-weight: 700; text-align: left;
                padding-bottom: 4px; border-bottom: 1.5px solid #bbb;
            }}
            .table-container td {{ padding-top: 6px; font-size: 12.5px; font-weight: 600; color: #222; }}
            .disclaimer {{
                position: absolute; bottom: 10px; left: 0; right: 0; text-align: center;
                font-size: 11px; color: #555; font-weight: 600;
                border-top: 1px solid #ccc; padding-top: 5px; margin: 0 20px;
            }}
        </style>
    </head>
    <body>
        <div id="card-front" class="card">
            <div class="header">
                <div>{header_logo_html}</div>
                <div class="header-title">फार्मर आयडी कार्ड</div>
            </div>
            <div class="content">
                <div class="photo-box">
                    {photo_tag}
                </div>
                <div class="details">
                    <div><span class="field-label">नाव :</span> <span class="name-mr">{data['name_mr']}</span></div>
                    <div class="name-en" style="padding-left: 38px;">{data['name_en']}</div>
                    <div class="field-row"><span class="field-label">जन्म दिनांक / DOB :</span> {data['dob']}</div>
                    <div class="field-row"><span class="field-label">लिंग / Gender :</span> {data['gender']}</div>
                    <div class="field-row"><span class="field-label">मोबाइल / Mobile :</span> {data['mobile']}</div>
                    <div class="field-row"><span class="field-label">आधार नं / Aadhaar No :</span> {aadhaar_display}</div>
                </div>
                <div class="qr-box">
                    <img src="data:image/png;base64,{data['qr_base64']}" />
                </div>
            </div>
            <div class="footer">
                <span class="footer-text">फार्मर आयडी</span>
                <span class="footer-id">{formatted_fid}</span>
            </div>
        </div>

        <div id="card-back" class="card card-back">
            <div class="sec-title">संपूर्ण पत्ता / Address</div>
            <div class="address-box">
                <div class="address-mr">{data['address_mr']}</div>
                <div class="address-en">{data['address_en']}</div>
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
                    <tr>
                        <td>{data['district']}</td>
                        <td>{data['taluka']}</td>
                        <td>{data['village']}</td>
                        <td>{data['survey_no']}</td>
                        <td>-</td>
                        <td>{data['area']}</td>
                    </tr>
                </tbody>
            </table>
            <div class="disclaimer">
                This card is only for personal use not for Govt. identity
            </div>
        </div>
    </body>
    </html>
    """

# ----------------- 6. ROBUST RENDER ENGINE -----------------

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

        page = browser.new_page(viewport={"width": 600, "height": 800}, device_scale_factor=2)
        page.set_content(html_content, wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        front_bytes = page.locator("#card-front").screenshot()
        back_bytes = page.locator("#card-back").screenshot()
        browser.close()
        return front_bytes, back_bytes

# ----------------- 7. UI ROUTING & STATE MANAGEMENT -----------------

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
        st.title("🌾 Agri Farmer ID Card Generator")
    with col_logout:
        if st.button("Sign Out"):
            st.session_state["authenticated"] = False
            st.session_state["card_front"] = None
            st.session_state["card_back"] = None
            st.session_state["farmer_data"] = None
            st.rerun()

    st.write("Upload your registration PDF, enter the Aadhaar number, and press **Generate Card**.")

    with st.form("card_generation_form", clear_on_submit=False):
        col_file, col_input = st.columns([2, 1])

        with col_file:
            uploaded_file = st.file_uploader("Upload Registration PDF", type=["pdf"])

        with col_input:
            user_aadhaar = st.text_input(
                "Aadhaar Number",
                placeholder="Enter 12-digit number",
                max_chars=14
            )

        submitted = st.form_submit_button("Generate Card")

    if submitted:
        if uploaded