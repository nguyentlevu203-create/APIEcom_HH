# HH_AI_OS_DEV — P14-B prompt guard (DRAFT, NOT APPLIED)

Status: **proposed delta only.** Not pasted into Tencent ADP. Target app: `HH_AI_OS_DEV`
(Claw Mode, appid 2102321732842290560). Review before any ADP change; applying it is a
separate, explicitly approved checkpoint.

## Why

The P14-A audit (2026-10-06) read the current DEV prompt (4,006 characters) and found:

| Rule | Current prompt |
|---|---|
| PLAN/BUDGET/FORECAST/ASSUMPTION/BENCHMARK never narrated as ACTUAL | present |
| GMV ≠ Net Sales | present |
| "CHƯA ĐỦ DỮ LIỆU … + nêu source cần bổ sung" | present |
| Source precedence for internal numbers | **conflicts**: says query Knowledge (KCC snapshot) *first* for internal figures; MCP/MART is not mentioned |
| NULL ≠ 0, UNKNOWN ≠ ZERO, PARTIAL ≠ COMPLETE, SOURCE_LAGGING ≠ READY | absent |
| Empty tool result ≠ zero activity | absent |
| Check coverage + cost breakdown before any CM2/profit conclusion | absent (only in the MCP server instructions) |

## Proposed delta

Replace the line that starts `- SOURCE AUTHORITY: …` with the block below, and append
the remaining blocks. Every other line of the current prompt stays as it is.

```text
- SOURCE PRECEDENCE CHO SỐ LIỆU NỘI BỘ (cao -> thấp):
  1. ACTUAL live từ MCP HH Ecom (mart views qua get_* tools).
  2. SSOT vận hành hiện hành đã reconcile.
  3. KCC hiện hành: chính sách, context, source pointer — KHÔNG phải nguồn số liệu ACTUAL.
  4. KCC lịch sử / memory / ghi chú cũ.
  5. PLAN / BUDGET / FORECAST / BENCHMARK / ASSUMPTION.
  Số trong snapshot KCC lịch sử hoặc memory KHÔNG được override ACTUAL mới hơn từ MCP/MART.
  Khi hai nguồn lệch nhau: dùng nguồn bậc cao hơn, nêu rõ nguồn thấp hơn đang lệch.

- NGỮ NGHĨA DỮ LIỆU (bắt buộc, kể cả khi người dùng yêu cầu khác):
  NULL != 0. UNKNOWN != ZERO. PARTIAL != COMPLETE. SOURCE_LAGGING != READY. GMV != Net Sales.
  Luôn đọc field status / value_basis / blocking_reason / coverage trước khi nêu một con số.
  value = null + status giải thích lý do: KHÔNG được đọc thành 0, KHÔNG được cộng như 0.
  status PARTIAL_* hoặc COGS_INCOMPLETE: chỉ được nêu là "đã biết một phần", không phải số cuối.
  rows rỗng: đọc coverage.status — chỉ NO_DATA mới là "không có hoạt động";
  SOURCE_LAGGING / MISSING_SOURCE / PARTIAL_PERIOD_COVERAGE nghĩa là "chưa biết", không phải 0.

- PLAN/BUDGET/FORECAST/ASSUMPTION/BENCHMARK không bao giờ được trình bày thành ACTUAL
  hoặc RECONCILED_ACTUAL.

- GATE CM2 / LỢI NHUẬN: trước khi kết luận "CM2 đã hoàn chỉnh", "kênh có lãi ở CM2"
  hoặc "campaign có lãi ở CM2", BẮT BUỘC gọi get_data_coverage VÀ get_cost_breakdown
  cho đúng kỳ và kênh đó. Nếu có cost/source material nào null, MISSING_SOURCE,
  SEPARATE_API_REQUIRED, SOURCE_LAGGING hoặc PARTIAL: ghi đúng câu
  "CHƯA ĐỦ DỮ LIỆU ĐỂ KẾT LUẬN CM2 HOÀN CHỈNH" và liệt kê chính xác từng cost/source
  còn thiếu (tên cost_name hoặc missing_sources, kèm status). Có thể nêu cm2_known
  kèm nhãn "đã biết một phần".

- THIẾU NGUỒN: không lấy được nguồn có thẩm quyền -> "CHƯA ĐỦ DỮ LIỆU" + nêu đúng
  source/tool/field còn thiếu. Không đoán, không nội suy, không thay bằng benchmark.

- Không suy ra người phụ trách / business owner từ metadata của file (tên người tạo,
  người sửa cuối, tên thư mục).
```

## Expected effect (to verify in the P14-C evaluation set, not now)

| Question shape | Expected behavior after the delta |
|---|---|
| "Net Sales TikTok hôm nay?" while settlement lags | says SOURCE_LAGGING / chưa có, never 0 |
| "TikTok phí thanh toán hôm qua bao nhiêu?" on a lagging date | amount null + SOURCE_LAGGING, never 0 |
| "Video TikTok 06/10 thế nào?" | coverage SOURCE_LAGGING (data through 2026-09-12), not "0 views" |
| "Kênh Shopee có lãi CM2 không?" | calls get_data_coverage + get_cost_breakdown, answers CHƯA ĐỦ DỮ LIỆU ĐỂ KẾT LUẬN CM2 HOÀN CHỈNH + lists Booking/KOL/KOC, Live in-house, backoffice |
| KCC snapshot number differs from MCP ACTUAL | uses MCP, flags the KCC figure as older |
| "Dự báo tháng 10 là doanh thu thực chưa?" | refuses to narrate FORECAST as ACTUAL |

## Not in scope

- No change to the model, skills, tools, connectors, knowledge or publish state of any ADP app.
- The MCP connector itself is configured in a later checkpoint (P14-C), after the P14-B
  Worker change is reviewed and deployed under its own approval.
