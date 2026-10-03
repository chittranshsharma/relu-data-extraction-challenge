# Disney Cruise Line Data Extraction & Analysis

> **Relu Consultancy Hiring Challenge — Data Extraction Engineer (FTE)**  
> **Challenge Objective 1**: Automated extraction, data cleaning, and analytical reporting from [Disney Cruise Line (`en-in`)](https://disneycruise.disney.go.com/en-in/).

---

## 📊 Challenge Answers (Summary)

The following metrics are computed directly from the cleaned dataset ([`results.csv`](results.csv)), strictly matching the live site counter (**948 Cruises**):

| # | Question | Primary Count (Itineraries) | Alternative Count (Sailing Dates / Counter) | Context & Clarification |
|---|---|:---:|:---:|---|
| **(i)** | **How many total cruises are there for the Pacific as a destination?** | **2** | **5** | 2 unique itinerary cards offering a total of 5 departure dates (3 + 2 = 5). |
| **(ii)** | **How many total cruises are there?** | **171** | **948** | 171 unique itinerary cards across all pages; the site counter displays `948 Cruises` (total sailing-date options across all cards). $\sum \text{num\_dates} = 948$. |
| **(iii)** | **How many holiday cruises are there?** | **44** | **134** | 44 itinerary cards with theme badge *"Very Merrytime"* / holiday keywords (134 sailing dates). *(Note: Disney categorizes "Halloween on the High Seas" separately with 27 cards).* |
| **(iv)** | **How many Cruises offer more than 2 dates for booking?** | **67** | — | 67 distinct cruise cards display `Show N Dates` where $N > 2$. |
| **(v)** | **How many cruises do Miami and London have as departure ports?** | **0** | **0** | Neither Miami nor London is an active Disney Cruise departure port in the catalog (ports include Port Canaveral, Fort Lauderdale, San Diego, Galveston, Southampton, etc.). |

---

## 🛠️ Tech Stack & Requirements

- **Python 3.10+**
- **Playwright** (browser automation and dynamic DOM scraping)
- **Pandas** (data manipulation, cleaning, deduplication, and export)

Install dependencies:
```bash
pip install -r requirements.txt
playwright install chromium
```

---

## 🚀 How to Run

To run the complete verified scraper (takes approximately 2–3 minutes):

```bash
python scraper.py
```

### Command-Line Options

```bash
python scraper.py --help
```
- `--channel chrome`: Launches the installed Google Chrome browser (default).
- `--delay 1.5`: Delay in seconds between scroll hydration steps (default: `1.5s`).
- `--headless`: Runs headless (Note: Disney's Akamai WAF returns HTTP 403 to headless Chromium).
- `--pacific-filter-pass`: Optionally executes a second pass applying the UI "Sailing to: Pacific Coast" filter.

---

## 🧭 Workflow & Implementation Architecture

The scraper strictly adheres to Steps 1–6 specified in the technical requirements:

1. **Step 1: Navigation & Consent**:
   - Opens `https://disneycruise.disney.go.com/en-in/` in visible Chrome to avoid Akamai WAF blocks.
   - Automatically detects and accepts cookies/consent dialogs if triggered.
2. **Step 2 & 3: Search & Filter Verification**:
   - Programmatically locates and clicks the **"View Dates"** button to load cruise listings.
   - Checks for pre-applied promo chips (e.g. seasonal banners) and clicks **"Clear All"** if detected, ensuring the complete unfiltered catalog is gathered.
   - Reads the dynamic live counter (`948 Cruises`) via `h2.available-cruises-wrapper__title`.
3. **Step 4: Infinite Scroll Card Harvesting**:
   - Cruises load via lazy hydration upon scrolling rather than numbered pages.
   - The script progressively scrolls down, taking DOM snapshots of custom elements (`div.product-card-wrapper`).
   - Destination ports are extracted directly from `.ul-port-list li` elements and joined cleanly with `" | "`.
   - Iterative checkpoints are written to `disney_cruises_raw.csv`.
4. **Step 5: Data Cleaning & Normalization**:
   - **No Empty Fields**: Explicit, standard markers are applied:
     - Absent badges $\rightarrow$ `"None"`
     - Missing destination (observed on 2 Singapore cards) $\rightarrow$ `"Not shown on card"`
     - Absent USD price $\rightarrow$ `"Not shown on card"` (prices are presented in INR on the `en-in` locale)
     - Card anchor link $\rightarrow$ `"#"` (raw href attribute preserved from `<a href="#">Show N Dates</a>`)
   - **Deduplication**: Deduplicated by composite identity: `(title, departure_port, price_usd, price_inr, num_dates)`.
   - **Strict Quality Gate**: Asserts $\sum \text{num\_dates} == \text{site\_total}$ (948) before generating the final CSV.
5. **Step 6: CSV Export & Analysis**:
   - Cleansed data is exported to `results.csv` with UTF-8 BOM encoding (`utf-8-sig`).
   - Terminal prints computed answers for the 5 evaluation questions.

---

## 📋 Data Schema (`results.csv`)

The output dataset contains **171 rows** and **11 columns** with 0 blank cells:

| Column | Data Type | Description | Sample Value |
|---|---|---|---|
| `title` | string | Full cruise itinerary name | `3-Night Baja Cruise from San Diego` |
| `theme_badge` | string | Seasonal/theme offer tag | `Very Merrytime`, `Halloween on the High Seas`, or `None` |
| `quality_badge` | string | Room booking restriction | `Guaranteed with Restrictions` or `None` |
| `duration_nights` | integer | Duration extracted from title | `3`, `4`, `7` |
| `departure_port` | string | Port parsed from title | `San Diego`, `Port Canaveral`, `Fort Lauderdale` |
| `sailing_to` | string | Destination stops separated by ` \| ` | `Catalina Island, California \| Ensenada, Mexico` |
| `price_usd` | string | Raw USD price text | `Not shown on card` |
| `price_inr` | string | Raw INR price text | `₹1,29,140 INR` |
| `num_dates` | integer | Number of booking dates available | `4` |
| `cta_link` | string | Raw CTA anchor link attribute | `#` |
| `scroll_step` | integer | Scroll iteration during hydration | `1`, `2`, `3` |

---

## 📁 Repository Structure

```text
.
├── scraper.py                 # Main production Playwright scraper & pipeline
├── results.csv                # Final cleansed dataset (171 rows × 11 columns)
├── disney_cruises_raw.csv     # Raw checkpoint dataset collected before cleaning
├── requirements.txt           # Python package dependencies
├── .gitignore                 # Standard exclusions (temp files, cache, backups)
└── README.md                  # Comprehensive project documentation
```
