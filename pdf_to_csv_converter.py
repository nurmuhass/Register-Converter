#!/usr/bin/env python3
"""
pdf_to_csv_converter.py
Convert HCP-like PDF lists to CSV. Heuristics-based parser with OCR fallback.

Updates:
- Ignore footer lines like "Page 1399 of 1402 - ...".
- Removed S/N and TotalEnrollees columns from output CSV.
- Preserve blank fields (e.g., EmpCode may be blank and won't cause shifting).
- Detect family headers: "Family XXXXX Code - 1419450" and "NHIA - GIFSHIP_* Batch 1468243".
- For GIFSHIP families: force Relationship=MEMBER and strip MEMBER from FirstName.
- Correctly handle EXTRA DEPENDENT 1–6 as valid relationship values.
"""

import re
import csv
import sys
import argparse
from pathlib import Path

# Primary text extraction
try:
    import pdfplumber
except ImportError:
    pdfplumber = None

# Optional OCR fallback
try:
    from pdf2image import convert_from_path
    import pytesseract
except ImportError:
    convert_from_path = None
    pytesseract = None

# Optional Camelot (table extraction) - only if installed
try:
    import camelot
except Exception:
    camelot = None

# -----------------------
# Parsing utilities
# -----------------------
RELATION_KEYWORDS = {
    "PRINCIPAL", "SPOUSE", "CHILD", "CHILD1", "CHILD2", "CHILD3",
    "CHILD4", "CHILD 1", "CHILD 2", "GUARDIAN", "DEPENDENT", "DEPENDANT"
}

# Regex to match EXTRA DEPENDENT 1–6
EXTRA_DEP_RE = re.compile(r'EXTRA\s+DEPENDENT\s+([1-9])', re.IGNORECASE)

DATE_RE = re.compile(r'^(?:\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\d{4}[\/\-]\d{1,2}[\/\-]\d{1,2})$')
SEX_RE = re.compile(r'^(?:M|F|MALE|FEMALE|Male|Female)$', re.IGNORECASE)
NHIA_RE = re.compile(r'^[0-9]{3,}[-]?[0-9]*$')  # e.g. 3024514-1

FOOTER_RE = re.compile(r'^\s*Page\s+\d+\s+of\s+\d+.*$', re.IGNORECASE)

# Regex patterns for family headers
family_re = re.compile(r'Family\s+.*?\s+Code\s*[-:]\s*(\d+)', re.IGNORECASE)
gifship_re = re.compile(r'NHIA\s*-\s*GIFSHIP[_A-Z]*\s+Batch\s+(\d+)', re.IGNORECASE)

def is_footer_line(line):
    return bool(FOOTER_RE.match(line.strip()))

def find_date_index(tokens):
    for i, t in enumerate(tokens):
        if DATE_RE.match(t.strip()):
            return i
    for i, t in enumerate(tokens):
        cleaned = t.strip().strip('.,')
        if DATE_RE.match(cleaned):
            return i
    return None

def find_nhia_index(tokens, max_scan=3):
    for i in range(min(len(tokens), max_scan)):
        if NHIA_RE.match(tokens[i].strip()):
            return i
    return None

def normalize_token(t):
    return t.strip().strip(',')

def parse_member_line(line):
    text = line.strip()
    if text == "":
        return None
    tokens = [normalize_token(t) for t in re.split(r'\s+', text) if t.strip() != ""]
    if len(tokens) < 3:
        return None

    header_like = ['S/N','NHIA','NO','NAME','RELATION','REL','SEX','DOB','EMP','EMP CODE','EMPLOYEE']
    if any(tok.upper() in header_like for tok in tokens[:4]):
        return None

    dob_idx = find_date_index(tokens)
    if dob_idx is None:
        return None

    dob_token = tokens[dob_idx].strip()
    sex = ""
    sex_idx = dob_idx - 1
    if sex_idx >= 0 and SEX_RE.match(tokens[sex_idx].strip()):
        sex = tokens[sex_idx].strip()
        name_end_idx = sex_idx
    else:
        name_end_idx = dob_idx

    if dob_idx + 1 < len(tokens):
        emp_code = " ".join(tokens[dob_idx + 1:]).strip()
    else:
        emp_code = ""

    nhia_idx = find_nhia_index(tokens, max_scan=3)
    if nhia_idx is None:
        return None

    nhia = tokens[nhia_idx].strip()
    middle_tokens = tokens[nhia_idx + 1:name_end_idx]
    relationship = ""
    firstname = ""
    lastname = ""

    if len(middle_tokens) == 0:
        relationship = ""
        name_tokens = []
    else:
        # --- handle CHILD 1, CHILD 2 ---
        if (len(middle_tokens) >= 2 and middle_tokens[0].upper() == "CHILD" and middle_tokens[1].isdigit()):
            relationship = " ".join(middle_tokens[0:2])
            name_tokens = middle_tokens[2:]
        # --- handle EXTRA DEPENDENT 1–6 ---
        elif len(middle_tokens) >= 3 and middle_tokens[0].upper() == "EXTRA" and middle_tokens[1].upper() == "DEPENDENT" and middle_tokens[2].isdigit():
            relationship = " ".join(middle_tokens[0:3])  # e.g. "EXTRA DEPENDENT 2"
            name_tokens = middle_tokens[3:]
        elif len(middle_tokens) >= 2 and " ".join(middle_tokens[0:2]).upper() in ("EXTRA DEPENDENT", "EXTRA DEPENDANT"):
            relationship = " ".join(middle_tokens[0:2])
            name_tokens = middle_tokens[2:]
        # --- handle standard RELATION_KEYWORDS ---
        elif middle_tokens[0].upper() in RELATION_KEYWORDS:
            relationship = middle_tokens[0]
            name_tokens = middle_tokens[1:]
        else:
            # fallback
            relationship = ""
            name_tokens = middle_tokens

    if len(name_tokens) == 0:
        firstname = ""
        lastname = ""
    elif len(name_tokens) == 1:
        firstname = name_tokens[0]
        lastname = ""
    else:
        firstname = " ".join(name_tokens[:-1])
        lastname = name_tokens[-1]

    return {
        'NHIA_Number': nhia,
        'Relationship': relationship,
        'FirstName': firstname,
        'LastName': lastname,
        'Sex': sex,
        'DOB': dob_token,
        'EmpCode': emp_code
    }

# -----------------------
# High-level PDF parsing
# -----------------------

def extract_text_with_pdfplumber(pdf_path):
    texts = []
    if pdfplumber is None:
        raise RuntimeError("pdfplumber not installed. Install with: pip install pdfplumber")
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            texts.append(page.extract_text() or "")
    return "\n".join(texts)

def extract_text_with_ocr(pdf_path, dpi=200, first_n_pages=None):
    if convert_from_path is None or pytesseract is None:
        raise RuntimeError("pdf2image/pytesseract not installed. Install them for OCR fallback.")
    pages = convert_from_path(pdf_path, dpi=dpi)
    texts = []
    for i, page_image in enumerate(pages):
        if first_n_pages and i >= first_n_pages:
            break
        text = pytesseract.image_to_string(page_image)
        texts.append(text)
    return "\n".join(texts)

def parse_document_text(text):
    rows = []
    current_provider = None
    current_provider_number = None
    current_family_code = None
    current_family_type = "NORMAL"   # NORMAL or GIFSHIP

    lines = [l.rstrip() for l in text.splitlines() if l.strip() != ""]
    provider_number_re = re.compile(r'Provider Number[:\s]*([A-Z0-9\-\/]+)', re.IGNORECASE)

    for line in lines:
        if is_footer_line(line):
            continue

        mprovnum = provider_number_re.search(line)
        if mprovnum:
            current_provider_number = mprovnum.group(1).strip()
            continue

        mfam = family_re.search(line)
        if mfam:
            current_family_code = mfam.group(1).strip()
            current_family_type = "NORMAL"
            continue

        mgif = gifship_re.search(line)
        if mgif:
            current_family_code = mgif.group(1).strip()
            current_family_type = "GIFSHIP"
            continue

        if any(k in line.upper() for k in ('HOSPITAL','CLINIC','CENTRE','CENTER','SPECIALIST','PROVIDER','HEALTH')):
            current_provider = line.strip()
            continue

        if re.search(r'^(S\/N|NHIA|NAME|RELATION|EMP CODE|EMPLOYEE|TOTAL ENROLLEES)', line, re.IGNORECASE):
            continue

        parsed = parse_member_line(line)
        if parsed:
            parsed['Provider'] = current_provider or ""
            parsed['ProviderNumber'] = current_provider_number or ""
            parsed['FamilyCode'] = current_family_code or ""

            if current_family_type == "GIFSHIP":
                parsed['Relationship'] = "MEMBER"
                if parsed['FirstName'].upper().startswith("MEMBER "):
                    parsed['FirstName'] = parsed['FirstName'][7:].strip()
                elif parsed['FirstName'].upper() == "MEMBER":
                    parsed['FirstName'] = ""

            rows.append(parsed)
    return rows

# -----------------------
# CSV output
# -----------------------
CSV_FIELDS = ['Provider','ProviderNumber','FamilyCode',
              'NHIA_Number','Relationship','FirstName','LastName','Sex','DOB','EmpCode']

def write_csv(rows, out_path):
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in rows:
            out = {k: r.get(k, "") for k in CSV_FIELDS}
            writer.writerow(out)
    print(f"Wrote {len(rows)} rows to {out_path}")

# -----------------------
# Command-line
# -----------------------
def main():
    ap = argparse.ArgumentParser(description="Convert HCP PDF to CSV (footer ignored, FamilyName removed, GIFSHIP handled, EXTRA DEPENDENT handled)")
    ap.add_argument('pdf', help="Path to PDF file")
    ap.add_argument('-o','--out', help="Output CSV path", default="output.csv")
    ap.add_argument('--ocr', action='store_true', help="Force OCR (use if pdfplumber extraction fails)")
    ap.add_argument('--ocr-pages', type=int, default=None, help="If using OCR, how many pages to process (default all)")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print("PDF not found:", pdf_path)
        sys.exit(1)

    text = ""
    used_ocr = False
    try:
        if not args.ocr:
            if pdfplumber is None:
                raise RuntimeError("pdfplumber not installed")
            print("Extracting text with pdfplumber...")
            text = extract_text_with_pdfplumber(pdf_path)
            if len(text.strip()) < 100:
                print("pdfplumber extraction returned little text; will fallback to OCR.")
                raise RuntimeError("empty extraction")
        else:
            raise RuntimeError("forced OCR")
    except Exception as e:
        print("Falling back to OCR:", str(e))
        if convert_from_path is None or pytesseract is None:
            print("OCR libs missing. Install pdf2image and pytesseract to enable OCR fallback.")
            sys.exit(1)
        used_ocr = True
        text = extract_text_with_ocr(pdf_path, first_n_pages=args.ocr_pages)

    print("Parsing text...")
    rows = parse_document_text(text)
    if len(rows) == 0 and not used_ocr and camelot:
        print("No rows found — trying camelot table extraction...")
        try:
            tables = camelot.read_pdf(str(pdf_path), pages='all')
            for t in tables:
                df = t.df
                for i in range(len(df)):
                    line = " ".join(df.iloc[i].tolist())
                    parsed = parse_member_line(line)
                    if parsed:
                        parsed['Provider'] = ""
                        parsed['ProviderNumber'] = ""
                        parsed['FamilyCode'] = ""
                        rows.append(parsed)
        except Exception as e:
            print("Camelot extraction failed:", e)

    write_csv(rows, args.out)

if __name__ == "__main__":
    main()
# Example run:
# python pdf_to_csv_converter.py "POLICE HEALTH MAINTENANCE LIMITED_224_1.pdf" -o output.csv
