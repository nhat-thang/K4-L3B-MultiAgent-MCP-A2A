# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

```text
Input → Entity Resolver → Coordinator → Specialists → Conflict Resolver → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP ────────────────┴──────────── Trace
```

Toàn bộ pipeline chạy trong một hàm `solve_case()` (`src/student_agent/workflow.py`), điều phối qua các
hàm agent riêng biệt chia sẻ một `CaseAgent` (gateway + trace + bộ nhớ evidence_ref/conflict trong phạm vi
một case). Mỗi case độc lập hoàn toàn: không cache hay evidence nào rò rỉ sang case khác.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| entity-agent | `candidate_order_ids`, `claimed_order_id`, `customer_unique_id_hint` | Xác thực từng candidate qua `get_order`; đối chiếu với `get_customer_history` để loại candidate không thuộc khách hàng | `get_order`, `get_customer_history` | `resolved_order_ids` / `rejected_candidates` → coordinator |
| coordinator | Primary order đã resolve | Điều phối handoff tuần tự tới order/shipment/payment/policy agent, không tự gọi tool nghiệp vụ | (không gọi MCP) | `handoff` events |
| shipment-agent | `order_id`, `order_purchase_timestamp` | Lấy `get_order_items`, `get_shipment_summary`; lọc row/event ngoài time-window của order; suy ra `shipment_analysis` | `get_order_items`, `get_shipment_summary` | `ShipmentAssessment` → payment-agent (order_item_total) và policy classifier |
| payment-agent | `order_id`, `order_item_total_brl` từ shipment-agent | Lấy `get_payment_timeline`, `get_refund_timeline` (best-effort); đối chiếu captured vs order total; suy ra `payment_analysis` | `get_payment_timeline`, `get_refund_timeline` | `PaymentAssessment` → policy classifier |
| policy-agent | `primary_issue` đã phân loại, `policy_version` | Gọi `get_policy`, tra cứu rule tương ứng để lấy `case_status`, `recommended_action`, `refund_brl`, `responsible_parties` xác thực từ server thay vì tự suy đoán | `get_policy` | `financial_resolution`, `resolution_actions`, `root_cause_analysis` |
| conflict resolver | Toàn bộ row/event thô từ order_items/shipment_summary/payment_timeline/refund_timeline | So khớp mốc thời gian của mỗi row với `order_purchase_timestamp` (anchor); row lệch ngoài window bị loại và ghi vào `data_conflicts` | (không gọi MCP riêng, chạy trong shipment/payment agent) | `data_conflicts[]` |
| verifier | Output nháp, danh sách evidence_ref đã thu thập | Emit `verification_completed` kèm `case_status`/số conflict đã phát hiện làm bằng chứng quan sát được | (không gọi MCP) | Trace event cuối cùng của case |

`get_sellers` và `get_product_context` được discover nhưng **chủ động không gọi**: thông tin của chúng (vị
trí seller, category sản phẩm) không map vào bất kỳ field bắt buộc nào của `l3b-output-v2` schema, nên gọi
thêm chỉ tốn quota mà không tăng điểm evidence/semantic — áp dụng đúng nguyên tắc least privilege và
"tránh gọi tool thừa" của README.

## 3. Entity resolution và A2A protocol

- Candidate set = `claimed_order_id` + `candidate_order_ids`, dedupe giữ thứ tự.
- Mỗi candidate được xác thực bằng một lời gọi `get_order`; lỗi `RuntimeError` từ gateway (tool
  không tìm thấy order) được coi là "candidate không tồn tại" → đưa vào `rejected_candidates`, không phải
  lỗi case.
- Nếu có `customer_unique_id_hint`, gọi `get_customer_history` một lần; order nào không xuất hiện trong
  lịch sử khách hàng bị coi là resolve sai (rejected) dù `get_order` trả về hợp lệ — đây là điều kiện xác
  nhận entity thứ hai, độc lập với việc order có tồn tại hay không.
- `status`/`confidence`: `resolved` (0.95 nếu được customer history xác nhận, 0.7 nếu không có hint để xác
  nhận) → `ambiguous` (0.35, nhiều candidate cùng được xác nhận) → `not_found` (0.0).
- Handoff giữa các actor là lời gọi hàm async tuần tự trong cùng process, tương quan bằng `case_id`
  truyền xuyên suốt; không có vòng lặp vì mỗi agent chỉ chạy đúng một lần cho mỗi case, không có cơ chế
  retry-tới-agent-khác. Timeout do `httpx` timeout ở tầng gateway (300s) đảm nhiệm.
- Case không resolve được order (`not_found`) sẽ dừng sớm sau bước entity resolution, không gọi thêm bất kỳ
  tool nghiệp vụ nào (tối ưu efficiency) và trả output tối thiểu hợp lệ với `case_status="needs_investigation"`.

## 4. Evidence và conflict lifecycle

- Mọi evidence đi qua `CaseAgent.fetch()`: gọi `gateway.call()` (đã tự validate theo
  `mcp-evidence-response-v1`), sau đó emit `tool_result_consumed` với đúng `evidence_ref`, rồi lưu ref vào
  danh sách evidence của case. Không có `evidence_ref` nào được tạo thủ công.
- Conflict lifecycle: các domain có nhiều row/event cho một order (order_items, shipment events,
  payment events, refund events) được lọc qua `_split_by_timeframe()` — giữ lại row có mốc thời gian nằm
  trong window `[purchase - 7 ngày, purchase + 180 ngày]`. Row bị loại được ghi vào `data_conflicts` với
  `field`, hai `sources` mô tả giá trị giữ lại/bị loại, và `resolution_code` giải thích lý do (ví dụ
  `dropped_row_outside_order_purchase_window`).
- `payment_analysis` so khớp `captured_total_brl` (từ event có timestamp) với tổng `price + freight_value`
  của các item đã được lọc hợp lệ (không so với payment row thô, vì payment row không có timestamp riêng
  để lọc contamination).
- Evidence không tái sử dụng giữa case: `CaseAgent` được tạo mới cho mỗi lời gọi `solve_case()`.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP tool trả lỗi (domain không có dữ liệu, ví dụ chưa có refund) | 0 (không retry) | Coi là "không có evidence cho domain đó", tiếp tục case với giá trị mặc định (`refunded_total_brl=0`, v.v.) | Không emit `tool_result_consumed` cho lời gọi thất bại |
| Entity not found/ambiguous | 0 | Trả output tối thiểu hợp lệ, `case_status="needs_investigation"` (not_found) hoặc confidence thấp (ambiguous) | `verification_completed` với `decision_code` = trạng thái entity resolution |
| Source conflict (row/event ngoài time-window) | n/a (không phải lỗi, là dữ liệu) | Loại row lệch, giữ row nhất quán với `order_purchase_timestamp` | Ghi vào `data_conflicts[]`, không có trace event riêng (quan sát được qua output) |
| `primary_issue` không có rule trong `get_policy` | 0 | Dùng `PRIMARY_ISSUE_FALLBACK` (`case_status="needs_investigation"`, `refund_brl=0`, `responsible_parties=[unknown]`) | `policy_decided` vẫn emit với `decision_code=primary_issue` |

Query budget: mỗi case gọi tối đa ~7 tool MCP (2 `get_order` cho candidate, 1 `get_customer_history`, 1
`get_order_items`, 1 `get_shipment_summary`, 1 `get_payment_timeline`, 1 `get_refund_timeline`, 1
`get_policy`); tool discovery (`list_tools`) chỉ gọi một lần cho toàn bộ run nhờ cache theo `id(gateway)`,
không lặp lại mỗi case. Không có cơ chế retry tự động — lỗi tool được xử lý như tín hiệu nghiệp vụ (thiếu
evidence) thay vì lỗi hệ thống cần thử lại, tránh gọi tool lặp vô ích.

## 6. Verification invariants

Trước khi trả kết quả, `solve_case()` đảm bảo:

- Output luôn được `contracts.validate_output()` kiểm tra ở tầng `cli.py` trước khi ghi file (hard gate).
- `evidence_refs` ở output chỉ chứa ref thực sự lấy được từ MCP (không có ref bịa).
- `case_id` output luôn khớp input (kiểm tra lại ở `cli.py`).
- `confidence` luôn trong [0, 1], giảm dần theo số `data_conflicts` phát hiện được (penalty tối đa 0.4).
- `rejected_candidates` ghi lại cả order không tồn tại lẫn order tồn tại nhưng không thuộc khách hàng.
- `financial_resolution`/`root_cause_analysis.responsible_parties` lấy trực tiếp từ `get_policy` (nguồn
  thẩm quyền), không tự tính refund_brl để tránh lệch với policy chính thức.
- Trace event cuối cùng của mỗi case luôn là `verification_completed`, mang `decision_code` phản ánh
  `case_status` cuối cùng để có thể kiểm chứng qua trace mà không cần đọc lại output.

## 7. Reproducibility

- Không dùng LLM/model ngẫu nhiên trong `solve_case()` — toàn bộ là logic rule-based tất định dựa trên dữ
  liệu MCP trả về, nên cùng input + cùng dữ liệu MCP sẽ luôn cho cùng output (không có random seed).
- Dependency: `pyproject.toml` (cài qua `pip install -e ".[dev]"`), Python ≥ 3.11.
- Concurrency: mỗi case xử lý tuần tự trong vòng lặp `for case_id in case_set.case_ids` của `cli.py`
  (không xử lý song song), dùng chung một `EvidenceGateway`/`ClientSession` cho toàn bộ run.
- Lệnh chạy: `day09 run` rồi `day09 validate`. Không ghi Team API Key vào output/trace (được
  `submission.py` kiểm tra bằng `SECRET_PATTERN` trước khi đóng gói).

## Ghi chú sửa lỗi starter kit

`src/student_agent/mcp_gateway.py` bản gốc truy cập `result.isError` / `result.structuredContent`
(camelCase) trên đối tượng `CallToolResult` của SDK `mcp`, nhưng SDK cài trong `.venv` expose các field này
dưới dạng snake_case Python attribute (`is_error`, `structured_content`) — camelCase chỉ là alias khi
serialize JSON-RPC, không truy cập được qua attribute access trong Python. Điều này khiến **mọi** lời gọi
MCP ném `AttributeError` trước khi kịp raise lỗi nghiệp vụ. Đã sửa để dùng đúng tên attribute; nếu không sửa,
`solve_case()` không thể gọi được bất kỳ tool nào.
