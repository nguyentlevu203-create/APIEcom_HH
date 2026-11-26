# Quy tắc hệ thống cũ `hh-lpm-etl` — lọc dữ liệu vào & gọi dữ liệu chuẩn ra

Nguồn: `github.com/nguyentlevu203-create/hh-lpm-etl` @ `42d18b1` (đọc toàn bộ ngày 2026-11-26).
Ký hiệu dẫn: `S`=`scripts/shopee_etl.py`, `T`=`scripts/tiktok_etl.py`, `N`=`scripts/nhanh_etl.py`,
`M`=`scripts/mt_gt_etl.py`, `C`=`etl_common.py`, `B45`/`B46`/`B461`/`B479`=`scripts/build_ceo_daily_pnl_package_v4_x.py`.

> **Vai trò của file này:** bộ nhớ nghiệp vụ, tức là HH đã định nghĩa từng con số như thế nào trong hệ thống cũ.
> **Không phải nguồn sự thật cho hệ thống API.** Khi mâu thuẫn, quy tắc trong `CLAUDE.md` và
> `artifacts/v0/P8_5_CANONICAL_BUSINESS_RULES.md` thắng. Hằng số % ở đây là **ước tính cũ**, phải kiểm lại
> với API trước khi dùng. Không copy code hay pattern `COALESCE(...,0)` của repo cũ.
> File không chứa số liệu kinh doanh, email người nhận, tên khách hay nhân viên, giá vốn SKU (repo này public).

---

## 1. Kiến trúc và luồng chạy

```
Export TAY (Excel/CSV) → data/<kênh>/<thư mục>        ← điểm duy nhất chưa tự động
  → 4 ETL (Nhanh → Shopee → TikTok → MT/GT), mỗi kênh chỉ ghi schema của nó
  → Postgres "DuLieu" (localhost:5433, máy Windows): nhanh.*, shopee.*, tiktok.*, mtgt.*, dim.*, etl.*, inventory.*, ops.*
  → view control_tower.* → 3 lớp đọc: API FastAPI, CEO Daily P&L package (JSON/MD → Excel), email
  → thành công thì chuyển file sang processed/<kênh>/...
```

| Việc | Lịch | Lệnh |
|---|---|---|
| 4 ETL + email scorecard nhân viên | hằng ngày (Task Scheduler) | `run_daily_all.ps1` |
| CEO Daily P&L package | **thủ công** | `run_daily_pnl_v4_7_9.ps1` (mặc định ngày D−1) |
| Gửi Excel CEO từng người | **thủ công** | `run_send_ceo_daily_individual.ps1 -ReportDate -XlsxPath` |
| Control Tower v2_10, Growth tuần/tháng v3.3 | thủ công | ngoài lịch |

- Phiên bản: CLAUDE.md ghi CURRENT = v4.7.7, nhưng preflight gửi mail **khóa cứng v4.7.9**
  (`scripts/ceo_send_preflight.py:59-60`). Thực tế mail CEO chỉ gửi được từ package v4.7.9.
- Không có script nào trong repo dựng đủ workbook 7 sheet. Repo chỉ vá block Top20 vào template có sẵn
  (`render_ceo_daily_top20_v4_7_8.py`), nên bước render Excel đang làm tay hoặc bằng AI.

## 2. Cơ chế nạp file chung (`etl_common.py`)

- **File được nhận** (C:46, 526-529): `.xlsx/.xls/.csv`, bỏ file bắt đầu bằng `~`, chỉ quét cấp 1 của thư mục, sắp theo (mtime, tên). File sau ghi đè file trước.
- **Dò dòng tiêu đề** (`read_smart_table`, C:476-504): thử dòng 0; nếu không có cột chứa từ khóa thì quét 30 dòng đầu. Header được bỏ BOM, xuống dòng, gộp khoảng trắng. **Không chuẩn hóa NFC.** Cột trùng tên chỉ giữ cột đầu.
- **`find_col` mờ** (C:507-523), chạy theo thứ tự: khớp exact → bằng nhau sau khi bỏ dấu → **chứa chuỗi con**. Nguy hiểm: từ khóa ngắn (`sl`, `ước tính`) có thể bắt nhầm cột.
- **Cột thiếu không làm lỗi mà tự điền "0"** (`require_columns`, S:444-447). Lỗi bị che thành số 0.
- **Tiền** (`parse_money_one`, C:311-358): dấu `(x)` hoặc `-` là số âm. Có cả `.` và `,` thì dấu cuối là thập phân. Dấu `.` hoặc `,` đứng một mình mà theo sau đúng 3 chữ số thì là phân cách nghìn. Không đọc được thì ra 0.0.
  TikTok ads dùng parser riêng `tiktok_money_one` (T:174-233): dấu `.` đứng một mình **luôn là thập phân**.
- **Ngày** (C:389-451): bỏ hậu tố `(UTC±n)`; nhận số serial Excel; ưu tiên **ngày-trước** (dd/mm/yyyy), không bao giờ thử tháng-trước. Giờ local không có múi.
- **Mã** (`clean_code`, C:246-294): NFC, bỏ BOM/zero-width/NBSP, `123.0`→`123`, mở dạng khoa học. Giữ hoa thường, dấu, gạch và khoảng trắng bên trong.
- **Batch**: `etl.batch_runs` (mỗi run 1 dòng, không chặn file trùng hash, chỉ ghi log), lỗi dòng ghi `etl.import_errors`.
  Pre-flight `assert_production_schema` kiểm tra bảng bắt buộc và `util.norm_code` không phá mã (C:76-109).
- **Lỗi thì không chuyển file**: mọi ETL đều chạy trong 1 transaction. Lỗi thì rollback và giữ file ở `data/`.
- **Bẫy idempotency (mọi kênh):** commission, settlement, affiliate chỉ join với đơn **có trong run hiện tại**.
  Nạp lại file đơn mà thiếu file settlement hoặc affiliate thì giá trị cũ bị **ghi đè về 0 hoặc về số ước tính**.

## 3. Lọc dữ liệu vào — theo kênh

### 3.1 Shopee (`S`, version `v4.51_price_evidence_gift_classifier_v4_7_9`)
- **Thư mục**: `data/Shopee/1_orders`, `2_affiliate`, `3_settlement`, `4_ads`.
- **Thứ tự chạy**: affiliate → settlement → orders → ads → ghi DB → chuyển file (S:971-989).
- **shop_label lấy từ tên file** (S:346-376):
  - chứa `mall` → `SHOPMALL`;
  - `sữa chua|bledina` → `SUA_CHUA`;
  - `đại lý` → `DAI_LY`;
  - còn lại bỏ các token chung; nếu rỗng → `SHOPMALL`.
- **Ads**: ngày **chỉ lấy từ tên file** (`shopmall_YYYY-MM-DD`; thiếu thì dừng cả run). Chi phí = Σ cột đúng tên `Chi phí`. Không dùng cột ngày trong file, không cộng CPC/CPM (S:632-675). ETL **không cộng VAT**; báo cáo đọc `cost_vnd_vat` (cột sinh trong DB, ×1.10, DDL không có trong repo).
- **Header bắt buộc khớp chính xác (thiếu thì dừng)**:
  - `SKU phân loại hàng` (seller_sku; **cấm** fallback sang `SKU sản phẩm` hay barcode);
  - `Mã giảm giá của Shop`;
  - `Người bán trợ giá`;
  - `Được Shopee trợ giá`;
  - `Tổng số tiền được người bán trợ giá`;
  - `Giá ưu đãi` (khớp NFC).
  - Cột mờ: mã đơn, trạng thái, `Ngày đặt hàng`, `Giá gốc`, `Số lượng`, các phí, `Tổng số tiền người mua thanh toán`.
- **Hủy**: trạng thái chuẩn hóa (NFC + casefold) == `đã hủy`. Chỉ cần 1 dòng hủy là cả đơn hủy (S:93-101, 504). Chữ `Đã huỷ` viết khác dấu **không** được tính là hủy.
- **Hoàn/trả**: không có logic riêng, coi như đơn thường.
- **Quà tặng (v4.7.9, theo bằng chứng giá)**: `Giá ưu đãi ≤ 0`; nếu ô trống thì xét `Giá gốc ≤ 0`. **Không dùng từ khóa tên SP** (classifier :91-100). Builder CEO phân loại lại từ `raw_payload` khi đọc.
- **Combo `A+B+C`**: tách theo `+`, giá vốn = Σ thành phần. Không có hệ số `x N`, không dùng bảng alias.
- **Cổng COGS (fail-closed)** áp cho đơn không hủy:
  - SP bán mà SKU trống → `MISSING_SELLER_SKU_FOR_PAID_ITEM`;
  - mã không có trong `dim.product_costs` → `MISSING_AUTHORITATIVE_COGS`;
  - khi đó **dừng cả run, không ghi `shopee.*`** (S:157-226, 699-730).
  - Đơn hủy thì `unit_cogs = 0`.
- **Kiểm tra trợ giá**: `|tổng trợ giá − người bán − Shopee| > 1` thì dừng (S:534-541).
- **Công thức** (theo đơn; P = Giá gốc, Q = Số lượng):
  - `gross_sales` = Σ P×Q của dòng **không phải quà**, **mọi trạng thái kể cả hủy**.
  - `seller_revenue` = 0 nếu hủy; ngược lại = Σ P×Q (tính cả quà) − Σ Người bán trợ giá − MAX(Mã giảm giá của Shop). Phần Shopee trợ giá **không trừ**.
  - Phí cố định/dịch vụ/thanh toán, `customer_paid` = MAX theo đơn (0 nếu hủy). Affiliate = Σ `Chi phí(₫)` của file affiliate (0 nếu hủy).
  - `return_fee` = 30.000 nếu hủy **và** có `Mã Kiện Hàng`.
  - `est_payout` = seller_revenue − 3 phí − affiliate − return_fee.
  - `payout_actual` = settlement `Tổng tiền đã thanh toán` nếu ≠ 0, ngược lại = est_payout (`payout_source`).
- **Bảng ghi**:
  - `shopee.orders` (khóa `external_order_id, shop_label`);
  - `order_items` (xóa rồi chèn lại);
  - `order_financials_extra`;
  - `settlements` (khóa thêm `settlement_date` theo tên file);
  - `ads_costs` (khóa `ads_date, shop_label`).

### 3.2 TikTok Shop (`T`)
- **Thư mục**:
  - `1_orders`, `2_affiliate_creator`, `3_affiliate_partner`, `4_settlement_paid`, `5_settlement_unpaid`;
  - `6_ads_product`, `7_ads_live`;
  - `8_master_data` (`Phí Book.xlsx`, `Nhóm Hàng.xlsx`; không chuyển đi);
  - `9_live_in_house`, `10_live_teaser`.
- **shop_label từ tên file**: chứa `LPM|PETIT|MARSEILLAIS` → `LPM`; chứa `SUA|CHUA|BLEDINA` → `SUA_CHUA`; mặc định `LPM` (T:433-440). `brand_group` đi theo shop_label (đã được chủ duyệt).
- **Đơn**:
  - Gộp dòng của mọi file theo (order_id, shop_label). **Đơn xuất hiện ở 2 file chồng nhau bị cộng đôi.**
  - Header bắt buộc khớp chính xác: `SKU Seller Discount`. `Shipped Time` khớp chính xác, tùy chọn.
  - `order_date` lấy từ `Created Time`.
- **Hủy**: trạng thái chuẩn hóa == `canceled` (tiếng Anh). Không xử lý Returned/Refunded.
- **Quà**: ETL không phân loại. Báo cáo coi **giá dòng = 0 là quà**.
- **Ads**:
  - Ngày và shop lấy từ tên file. Cột chi phí đúng tên `Chi phí` hoặc `Cost`; cấm `Chi phí ròng`/`Net Cost`/`Cost per…`.
  - Lưu `cost_vnd` **trước VAT**, `vat_rate = 0.10`; DB sinh `cost_vnd_vat`.
  - Sai thư mục (live/product) thì dừng.
  - `ads_type` = PRODUCT (thư mục 6) hoặc LIVE (thư mục 7).
- **Live**: in-house **không cộng VAT**; teaser **+10% VAT trong code** (T:624-676). Upsert ghi đè cả 2 cột; run thiếu một loại thì loại đó về 0.
- **SKU / COGS**:
  - Bảng alias `config/product_sku_alias_master_v4_7_6.csv` (chỉ dòng APPROVED, EAN 8-14 số) giúp mượn giá của EAN.
  - Combo tách theo `+`, mỗi phần có thể có hệ số `x N`.
  - **Chỉ combo mới bị chặn** khi thiếu giá; **SKU đơn thiếu giá thì im lặng ra COGS 0** (v4.7.9 tính lại giá khi đọc).
- **Phí (ước tính theo tỷ lệ, KHÔNG phải số thật API)**. Đặt `fee_base` = Σ(giá×SL) − Σ|SKU Seller Discount|:

  | Phí | Quy tắc cũ |
  |---|---|
  | Phí sàn (fixed) | LPM 15,8% (≤08/05/2026) → **21,54%** (từ 09/05); SUA_CHUA 10,31% → 15% (comment trong code ghi 17,8%, sai so với hằng số) |
  | Thanh toán | 6% → **6,6% từ 01/08/2026** |
  | Hạ tầng | 3.000 → **3.300 đ/đơn từ 01/08/2026** (cố định theo đơn) |
  | VXP | 5% fee_base (chưa áp trần 50.000đ/SP) |
  | Đóng gói | 2.000 đ × SL, **tính cả đơn hủy** |
  | Hoa hồng affiliate | creator: `ước tính tiêu chuẩn` nếu > 0, không thì `ước tính QC` (0 nếu "Không đủ điều kiện") + partner: Σ các cột exact được duyệt |
  | Booking KOL | fee_base × tỷ lệ trong `Phí Book`; chỉ khi `Loại nội dung` = `Phát trực tiếp` **và đúng 1 creator** khớp; **vẫn tính khi đơn hủy** |
  | Phí hoàn | 30.000 nếu hủy **và** có `Shipped Time` |
  | Backoffice | 15% × (settlement paid, nếu không có thì unpaid, nếu không có thì ước tính) |
  | Đơn hủy | phí sàn/thanh toán/VXP/hạ tầng/COGS/BO = 0; vẫn giữ đóng gói, booking, hoa hồng |

- **Bảng ghi**:
  - `tiktok.orders_pnl` (khóa `external_order_id, shop_label`);
  - `order_items`;
  - `ads_costs` (`ads_date, shop_label, ads_type`);
  - `live_costs` (`live_date, shop_label`).

### 3.3 Nhanh.vn (`N`)
- **Thư mục**: `data/nhanh/orders`, `data/nhanh/ads`.
- **Một file = một transaction.** Đơn upsert theo `external_order_id`, **không bao giờ xóa** đơn cũ.
- **Header chính xác (chỉ trim)**:
  - `Nhãn`;
  - `Giá trị đơn hàng` (= doanh thu thuần; **cấm** fallback sang Tổng tiền/Khách phải trả);
  - `Phí vận chuyển`;
  - `Phí ship báo khách`.
- **Kiểm tra trước khi forward-fill** (N:157-204): dòng tiêu đề đơn phải có `Nhãn` thuộc whitelist và `Giá trị đơn hàng` không trống. Sau đó mới forward-fill, và chỉ trong phạm vi cùng một ID.
- **Whitelist Nhãn** (12 nhãn = 7 chuẩn + 5 alias v4.7.7, ví dụ `FB - LPM Vietnam`, `OA - LPM Vietnam`, `INSTAGRAM`, `landing page`, `Web - lepetitmarseillais.vn`…). Nhãn khác bị loại ngay ở ETL.
- **Trạng thái** (NFC + casefold, khớp chính xác; `Đã huỷ` ≠ `Đã hủy`):
  - **Hủy**: `Đã hủy`, `Hệ Thống hủy`, `Khách hủy`, `HVC hủy`.
  - **Hoàn** (không tính là hủy): `Đang hoàn`, `Xác nhận hoàn`, `Đã hoàn`.
  - **Thất bại**: không tính là hủy, vẫn tính doanh thu và COGS (đang chờ chủ quyết).
  - **Không tính BO**: tập hủy + `Đã hoàn`.
- **Công thức**:
  - Net = `order_value` (mọi trạng thái).
  - Gross = Σ SL×giá dòng.
  - Ship = `Phí vận chuyển − Phí ship báo khách + 25.000` nếu `Đã hoàn`.
  - Quà khi `giá = 0`; nếu thiếu cột giá thì mọi dòng thành quà.
  - Combo tách `+` nhưng **phần thiếu giá tính 0** (không fail-closed).
  - Đóng gói 2.000 × SL (mọi trạng thái).
  - BO 15% × order_value dương, trừ tập không tính BO.
- **Ads**: ngày lấy từ cột, nếu không có thì từ tên file. Upsert theo (ngày, nhãn) và **ghi đè**.

### 3.4 MT / GT (`M`, sổ bán kế toán)
- **File**: `data/mt_gt/1_sales_ledger/01_MM_YYYY-GTMT.xlsx`, **1 file cho 1 tháng**, tên phải bắt đầu bằng ngày `01`.
- **Kiểm tra kỳ**: mọi `Ngày chứng từ` phải nằm trong tháng, không thì dừng. Cách nạp: xóa dữ liệu của tháng rồi chèn lại (snapshot tháng).
- **Master** `2_master_data`, nhận diện theo chuỗi con trong tên file:
  - `phân loại|kenh` → map kênh;
  - `nhân viên` → map nhân viên;
  - `khách` → map khách;
  - `ads|chi phí` → ads.
  - File tên `Kênh.xlsx` (có dấu), `CU.csv` **bị bỏ qua**.
- **Kênh**, xét lần lượt: map[`Tên nhóm khách hàng`] → map[`Mã công trình`] → map nhân viên → `OTHER`. Chỉ đúng chữ `MT`/`GT` (phân biệt hoa thường); `SI`/`Sỉ` thành OTHER.
- **Số lượng**: `quantity` = SL bán + SL KM − SL trả.
  **Doanh thu**: **`gross_revenue` = Doanh số bán × 1,08**; `net_revenue` = `Tổng thanh toán` (đã gồm VAT; âm khi trả hàng). Mâu thuẫn đang mở `GTMT_VAT_BASIS`.
- **Dòng CVC** (chi phí vận chuyển trên chứng từ): SL/doanh thu/COGS = 0, `cvc_amount` = |Tổng thanh toán|.
- **Quà**: chỉ đổi SL bán thành SL KM khi giá, doanh số và thanh toán đều = 0 **và** mã có trong `config/non_revenue_gift_skus.csv` (APPROVED).
- **Combo**: `config/bundle_component_map_v4_7_6.csv` (chỉ dòng `approved`); thành phần SOLD thiếu giá thì dừng cả run.
- **Chi phí khi báo cáo**: vận chuyển 2% Net, BO 15% Net, đóng gói 0, booking 0.
- **NV001**: hiển thị tách khỏi GT (`GT_NV001`), ads ép = 0, tổng công ty không đổi (v4.7.5).

## 4. Gọi dữ liệu chuẩn lên — tầng đọc / báo cáo

### 4.1 Ngày và nguồn
- **Ngày báo cáo** D = hôm qua.
- **Lũy kế tháng (MTD)** = ngày 1 → D, mọi ngày đều được điền đủ (ngày không bán = 0).
- **Ngày so sánh**: D−1 (mặc định), D−7, hoặc cùng ngày tháng trước.
- **Previous MTD**: ngày 1 tháng trước → cùng ngày, dựng lại bằng builder v4.6.1.
- Lọc theo cột ngày của từng bảng:
  - `order_date` (nhanh.orders / shopee.orders / tiktok.orders_pnl);
  - `ads_date`, `live_date`, `sale_date` (mtgt);
  - tồn kho = `MAX(snapshot_date) ≤ D`;
  - chỉ tiêu = `ops.monthly_sales_targets WHERE target_month = ngày 1`.
- **Nguồn chính**: `control_tower.v_scorecard_by_channel_daily_for_package` (Net Sales và tổng COGS mọi kênh; nguồn duy nhất cho GT/MT). View gốc `v_scorecard_by_channel_daily` **không có trong repo**.
  Chi tiết lấy trực tiếp từ bảng kênh: `shopee.orders ⟕ order_items ⟕ order_financials_extra`, `tiktok.orders_pnl ⟕ order_items`, `nhanh.*`, `*.ads_costs`, `tiktok.live_costs`, `mtgt.sales_lines`.

### 4.2 Lọc lúc đọc (tính lại từ trạng thái gốc, không tin cờ `is_cancelled` đã lưu)
- **Shopee**: `lower(regexp_replace(btrim(status),'\s+',' ')) = 'đã hủy'`.
- **TikTok**: cùng cách chuẩn hóa, `order_status` = `canceled`.
- **Nhanh**: tập 4 trạng thái hủy (v4.6.1 có NFC + không phân biệt hoa thường).
  - `success_orders` = **đơn không hủy**, phải ghi nhãn "Đơn không hủy", không được gọi là "đơn thành công".
- **Gross gồm mọi trạng thái**; phần đơn hủy là một dòng trừ riêng.
- **Kênh hiển thị**: GT, MT, NHANH, SHOPEE, TIKTOK. Kênh hoặc leaf chỉ hiện khi MTD có doanh số.
- **NHANH**: parent = tổng mọi nhãn (con số tài chính thật), **không bao giờ bằng Σ leaf**. Nhãn lạ vào `_UNALLOCATED`, nhóm này không bao giờ hiển thị và không được phân bổ lại. Cổng: |Parent − Leaf − Unallocated| ≤ 1đ.

### 4.3 Thác P&L CEO (`compose_pnl`, B45:893-1087; v4.7.9 sửa phần Shopee)
Chi phí lưu dương; dòng cuối là **LỢI NHUẬN**; **không có EBITDA**. Số gồm VAT, trừ khi truyền `--vat-rates`.

| # | Dòng | Công thức / nguồn |
|---|---|---|
| 1 | Gross Sales (mọi trạng thái) | Shopee/TikTok: Σ gross của detail; Nhanh: Σ SL×giá; GT/MT: lấy từ view |
| 2 | Doanh số đơn ĐÃ HỦY | Σ gross của đơn hủy |
| 3 | Chiết khấu người bán | Shopee: Σ `Người bán trợ giá` của dòng SOLD, đơn không hủy; TikTok: Σ max(gross − fee_base, 0) |
| 4 | Voucher Shop | chỉ Shopee: `Mã giảm giá của Shop` (đơn không hủy); voucher do sàn tài trợ **bỏ qua** |
| 5 | Điều chỉnh khác | phần dư = (gross − net) − (hủy + chiết khấu + voucher); \|x\| < 1 thì về 0 |
| 7 | **Net Sales** | lấy từ view; Shopee = `seller_revenue`; Nhanh = Σ order_value |
| 8-9 | Giá vốn bán / Giá vốn quà | SL × unit_cogs (`dim.product_costs`); gom lại phải khớp tổng COGS của view |
| 10 | **GM1** | Net − COGS |
| 11-19 | Phí cố định/dịch vụ/thanh toán/VXP/hạ tầng, đóng gói, logistics, ship hủy | Shopee/TikTok lấy từ bảng đơn; Nhanh: `shipping_fee`; GT/MT: logistics từ view (≈2% Net) |
| 20 | **CM1** | GM1 − (11..19) |
| 21-24 | Ads sản phẩm / live / teaser / khác | TikTok tách theo `ads_type`; teaser từ `live_costs`; kênh khác = `cost_vnd_vat` |
| 28 | Affiliate | Shopee `affiliate_amount`; TikTok creator + partner. **Không trừ thêm `platform_fees` cũ** (đã chứa affiliate) |
| 29 | Booking KOL/KOC | `tiktok.orders_pnl.booking_fee` |
| 30 | Livestream in-house | `tiktok.live_costs.in_house_cost` |
| 31-33 | Content / trade marketing / agency | không có nguồn, luôn = 0 |
| 34 | **CM2** | CM1 − (ads + affiliate + booking + live + 31..33) |
| 35 | Backoffice | lấy từ view; Nhanh 15% order_value; TikTok `backoffice_cost` |
| 36 | **LỢI NHUẬN** | CM2 − BO |

- **Margin** = chỉ số ÷ Net Sales.
- **Chỉ tiêu**: % đạt = MTD net / chỉ tiêu tháng; còn thiếu; cần mỗi ngày; dự báo cuối tháng. TOTAL = 5 kênh, **không cộng thêm ECOM**.
- **Không có nhãn ACTUAL/ESTIMATE từng dòng.** Chỉ có `data_quality.status` (PASS/WARNING/ERROR) + `can_send`, và danh sách mâu thuẫn mở `config/ceo_pnl_open_conflicts_v4_7_9.csv` (giữ nguyên số, chỉ cảnh báo).
- Chuỗi `N/D`/`N/A` bị cấm: không có doanh số thì ghi 0, thật sự không áp dụng thì để null.

### 4.4 Email scorecard nhân viên (SQL riêng, không qua package)
- Hàng theo chỉ tiêu `ops.monthly_sales_targets`: kênh không có chỉ tiêu thì không hiện.
- "LN còn lại" = **Tiền thu (payout)** − giá vốn − vận chuyển − BO − live − booking − QC − bao bì. Dựa trên payout, **không dựa trên Net Sales**.
- Shopee/TikTok dùng cờ `is_cancelled` đã lưu; Nhanh dùng danh sách hủy v2.12 (có `Đã hoàn`, thiếu `HVC hủy`), tức là **lệch với package CEO**.
- Người nhận đọc từ `auth.email_report_recipients`, quyền xem lợi nhuận theo `can_view_profit`.

## 5. Cổng chặn trước khi gửi
1. **`run_daily_all.ps1`**: thiếu env thì dừng. 4 ETL chạy fail-fast; **EMAIL HARD GATE** yêu cầu mọi bước PASS mới gửi, không dùng lại dữ liệu cũ.
2. **Builder**: exit 1 khi `can_send = false`. Một trong các lỗi sau sẽ làm `can_send = false`:
   - thiếu chỉ tiêu hoặc tồn kho;
   - ECOM ≠ SHOPEE + TIKTOK;
   - trạng thái Nhanh giống "hủy" mà chưa được map;
   - GT ≠ GT hiển thị + NV001;
   - NV001 có ads;
   - lệch Nhanh parent/leaf;
   - Top20 chưa map đủ;
   - đối soát dữ liệu raw lệch > 1đ.
3. **Validator** kiểm các đẳng thức:
   - Gross = đơn không hủy + đơn hủy; Gross − các khoản trừ = Net;
   - sold + gift = tổng COGS; Net − COGS = GM1; … ; CM2 − BO = Profit;
   - không có EBITDA, không có `N/A`, đúng thứ tự sheet.
4. **`ceo_send_preflight.py`** (không có cách bỏ qua):
   - ETL_GATE: mỗi job có batch mới nhất `success`, `rows_loaded > 0`, chạy sau 00:00 ngày D+1;
   - PACKAGE_GATE: đúng đường dẫn v4.7.9, đúng ngày, `can_send`, package được dựng sau ETL;
   - WORKBOOK_GATE: tên file `Báo cáo ngày DD-MM-YYYY.xlsx`, mới hơn package.
5. **Sender**: xlsx ≤ 18MB, đúng 7 sheet `00_Tong_quan…06_Ton_kho_Can_date`, không có chữ `ebitda`; chống gửi trùng bằng sha256(ngày | file | email).

## 6. Đối chiếu với hệ thống API hiện tại (để tích hợp)

| Khái niệm | Hệ thống cũ (file) | Hệ thống API (`mart.v_ceo_ecom_daily`) | Ghi chú |
|---|---|---|---|
| Hủy | `Đã hủy` / `Canceled` (nhãn export) | `order_status='CANCELLED'` (enum API) | cùng nghĩa, khác chuỗi |
| Gross / GMV | Σ giá gốc×SL, mọi trạng thái, trừ quà (Shopee) | `platform_gmv` = Σ total_amount mọi trạng thái | **không giống hệt**, cần đối soát |
| Net Sales Shopee | `seller_revenue` = gross_all − người bán trợ giá − voucher shop | escrow: original_price − hủy − seller_discount − voucher_from_seller + refund | gần nghĩa, khác nguồn (export vs escrow) |
| Net Sales TikTok | fee_base = giá×SL − SKU Seller Discount (đơn không hủy) | Σ settlement `revenue_amount` MATCHED | API theo settlement thật, có độ trễ |
| Phí TikTok | **% ước tính** | số thật `fact_settlement_sku_fee` | API thắng (P6B.2 chứng minh % cũ sai) |
| Quà | Shopee: giá ưu đãi ≤ 0; TikTok/Nhanh: giá = 0 | vai trò sản phẩm trong master (PROMO_GIFT) | khác cơ chế |
| Giá vốn | `dim.product_costs` (file "Giá nhập") | `core.dim_cogs` (`HH_OFFICIAL_COGS`) | cùng gốc "Giá nhập VND-cont" |
| Đóng gói | 2.000 × SL | 2.000 × SL đơn không hủy + giá vốn túi | TikTok cũ tính cả đơn hủy |
| Affiliate Shopee | Σ `Chi phí(₫)` file AMS | settlement AMS fee nếu có, không thì AMS conversion | API chính xác hơn |
| Ads | file, VAT 10% qua `cost_vnd_vat` | API Shopee ads / TikTok GMV Max | **API chưa quyết VAT ads**; cần chốt để khớp |
| Booking KOL, Live in-house/teaser | có (master + file 9/10) | **không tính** | **ngoài phạm vi CM2 theo quyết định 26/11/2026** — chỉ để tham khảo |
| Backoffice | 15% payout / 15% Net (GT/MT) | chưa có quy tắc (Profit = NULL) | HH phải quyết |
| Thiếu dữ liệu | thường điền 0 | NULL + trạng thái | không đổi hướng API |
| Nhanh, MT/GT | có | không có API | giữ đường file |

**Hướng tích hợp đã đề xuất:** repo cũ *đọc* Shopee/TikTok từ mart API (kéo `v_ceo_ecom_daily` bằng role chỉ đọc về schema riêng trong DuLieu, builder có cờ chọn nguồn và giữ đường file làm dự phòng). Nhanh và MT/GT giữ nguyên. Bắt buộc đối soát 1–2 tuần trước khi đổi nguồn trong báo cáo CEO.

## 7. Điểm mâu thuẫn / bẫy đã biết trong repo cũ
- Phí sàn TikTok: comment ghi 17,8%, hằng số là 21,54%; docstring nói "từ 08/05", code dùng `> 08/05`.
- GT/MT gross: code ×1,08, comment nói "lấy trực tiếp", danh sách mâu thuẫn nói là trước VAT (`GTMT_VAT_BASIS`).
- Tập trạng thái Nhanh khác nhau giữa các tầng: view v2.12 và email (có `Đã hoàn`, thiếu `HVC hủy`) so với ETL và v4.6.1.
- SKU đơn TikTok và combo Nhanh thiếu giá vẫn ra COGS 0 (không fail-closed); chỉ Shopee và combo MT/GT bị chặn.
- Cột thiếu tự điền "0"; `find_col` khớp chuỗi con nên có thể bắt nhầm cột.
- Nạp lại file đơn mà thiếu file settlement hoặc affiliate thì số cũ bị về 0 hoặc về số ước tính.
- API routers (`api/routers/*.py`) dùng `COALESCE(...,0)`, cộng affiliate vào platform_fees, BO = 15% payout. **Không an toàn, không dùng.**
- Ba nơi tính P&L (API, view SQL, package) không đảm bảo khớp nhau.
- Số dòng code được dẫn trong CLAUDE.md của repo cũ đã cũ (TikTok VXP, booking, ads VAT; Nhanh; MT/GT).
- Mâu thuẫn mở chờ HH/CFO quyết:
  - dòng "trả bằng hàng" của MT: tính vào COGS hay trade marketing;
  - đơn Nhanh `Thất bại` có SL quà lớn;
  - cơ sở VAT của GT/MT;
  - SKU hộp quà (stock hay non-stock).

## 8. Quy tắc làm việc cho Claude
1. Câu hỏi "HH tính X thế nào ở báo cáo cũ / Excel CEO" → tra file này trước, rồi mở repo cũ (clone `--depth 1` vào scratchpad, xong thì xóa) để kiểm dòng code cụ thể.
2. Câu hỏi về số hiện tại → **chỉ dùng mart API**. Số từ hệ thống cũ chỉ để so sánh và luôn ghi rõ nguồn.
3. Không đưa hằng số %, `COALESCE 0`, quy tắc quà theo từ khóa, hay BO 15% của repo cũ vào Gold/mart nếu HH chưa duyệt riêng.
4. Khi so số giữa hai hệ thống, nêu rõ khác biệt định nghĩa (mục 6) trước khi kết luận số nào sai.
5. Repo cũ cập nhật thường xuyên (version v4.7.x). Trước khi dựa vào một quy tắc, kiểm `git log` của repo cũ và cập nhật file này kèm commit mới.
