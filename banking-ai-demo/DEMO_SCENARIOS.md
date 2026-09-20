# MSB AI Financial Guardian — Kịch bản Demo

Tổng hợp các luồng demo đã sẵn sàng, kèm câu nói/thao tác chính xác và kết quả
mong đợi trên dữ liệu thật. Mọi con số dưới đây lấy từ khách demo **CIF 100008**
và đã được kiểm chứng trực tiếp trên cụm.

---

## 0. Truy cập

| | Địa chỉ | Đăng nhập |
|---|---|---|
| **App khách** | http://49.213.73.28.nip.io | SĐT `0950759303` / mật khẩu `123456` |
| **Console Agent** | http://49.213.73.37.nip.io | `admin@msb.com.vn` / `123456123456` |
| **Màn Ops nội bộ** | http://49.213.73.28.nip.io/ops | (tài khoản vận hành riêng — xem mục 5) |

Khách 100008: persona **cao tuổi**, thu nhập ~7,6 triệu/tháng, để dành thực tế
~**1.018.000 ₫/tháng**, có **28.679.000 ₫** trong tài khoản, **chưa có sổ tiết kiệm**.
Màn hình hiển thị tên "Nguyễn Việt Anh / Minh Anh".

---

## 1. Financial Copilot — trợ lý tài chính (chat)

**Vào:** Trang chủ → khối "Trợ lý AI Guardian" hoặc `/copilot/chat`.
**Chạy bằng:** agent **MSB Copilot** (`01a0a9fa`, model `glm-5.2`) trên Agent Platform,
gọi 19 công cụ domain. Mỗi câu trả lời có bảng số do gateway/agent dựng từ dữ liệu thật.

### 1.1. Chi tiêu tháng
- **Gõ:** `tháng này tôi tiêu nhiều nhất vào đâu`
- **Kết quả:** bảng theo nhóm — Hỗ trợ gia đình 1.600.000 ₫ (65%), Sức khoẻ 811.000 ₫,
  Ăn uống 54.000 ₫; **tổng chi sinh hoạt 2.465.000 ₫**. Agent còn nhận ra khoản
  **chuyển đi 620.150.000 ₫** không phải chi tiêu và nhắc kiểm tra an toàn.
- **Điểm nhấn:** phân biệt được chi tiêu thật vs chuyển khoản, số khớp database.

### 1.2. Mục tiêu tiết kiệm dài hạn ⭐
- **Gõ:** `giúp tôi tiết kiệm 500 triệu trong 3 năm tới`
- **Agent gọi 3 công cụ nối tiếp:** `review_quarter_spending` → `get_savings_capacity`
  → `plan_savings_goal`.
- **Kết quả:** nói thẳng **KHÔNG đạt được** — để dành 1.018.000 ₫/tháng, cần tới
  **12.672.987 ₫/tháng** (gấp ~12 lần), đạt 8% mục tiêu, giữ nhịp này mất **20,5 năm**.
  Kèm **4 bảng**: tóm tắt con số · rổ chi tiêu (thiết yếu/cam kết/co giãn) · phương án
  thay thế · gói tiết kiệm thật (lãi 6,2% / 6,0% / 5,9%).
- **Điểm nhấn:** cố vấn trung thực, lãi kép tính bằng biểu lãi thật (không để mô hình tự nhân).

### 1.3. Khám sức khỏe tài chính ⭐
- **Gõ:** `tình hình tài chính của tôi có ổn không`
- **Agent gọi:** `get_portfolio` → `check_financial_health`.
- **Kết quả:** **58 điểm — Khá**, bảng 5 trụ cột với đèn:
  | Trụ cột | Đèn | Nhận xét |
  |---|---|---|
  | Tỷ lệ tiết kiệm | ⚠️ | 13% thu nhập |
  | Quỹ dự phòng | ✅ | đủ ~10,3 tháng chi thiết yếu |
  | Gánh nặng chi cố định | 🔴 | 77% thu nhập |
  | Tiền nhàn rỗi | ⚠️ | 11.957.000 ₫ không sinh lời → **mất ~705.463 ₫/năm** |
  | Đa dạng tài sản | 🔴 | 100% tiền mặt, chưa có sổ tiết kiệm |
- **Điểm nhấn:** một câu hỏi, agent quét toàn cảnh và chấm điểm; điểm do hệ thống tính.

### 1.4. Sức chống chịu trước biến cố ⭐
- **Gõ (mất thu nhập):** `nếu tôi mất thu nhập vài tháng thì trụ được bao lâu`
  → **đủ sức**, trụ ~10,3 tháng nếu chỉ chi thiết yếu (4,4 tháng nếu giữ nguyên nếp chi).
- **Gõ (cần gấp):** `lỡ tôi ốm phải lo gấp 50 triệu thì có trụ được không`
  → **rủi ro**, thiếu **21.321.000 ₫**, gợi ý vay cầm cố thay vì bán tháo.
- **Agent gọi:** `get_portfolio` → `check_resilience`.
- **Điểm nhấn:** mô phỏng tương lai bất định, rất chạm với khách cao tuổi.

### 1.5. Câu phụ (nếu còn thời gian)
- `so sánh chi tiêu mấy tháng gần đây` — bảng nhiều tháng.
- `dự báo số dư cuối tháng` — `get_cashflow_forecast`.

---

## 2. Chat Banking — chuyển tiền bằng một câu nói

**Vào:** Trang chủ → "Chat Banking" hoặc `/chat-banking`.
**Chạy bằng:** agent **Chat Banking** (`01a0b01a`, model `qwen3.6-flash`, không gắn
công cụ nên chỉ ~2 giây). Số tiền do **code** tính (không để mô hình tự nhân), tên
người nhận đối chiếu **danh bạ thật** của đúng khách đang đăng nhập.

- **Gõ:** `chuyển cho anh Sơn 2 triệu rưỡi`
  → thẻ soạn lệnh: **2.500.000 ₫** → **MAI VAN SON**.
- **Gõ:** `gửi Trung 20 triệu` → 20.000.000 ₫ → NGUYEN VAN TRUNG.
- **Gõ:** `danh bạ của tôi` → liệt kê người thụ hưởng.
- **Điểm nhấn:** hiểu tiếng lóng số tiền ("rưỡi", "củ", "2tr5"), khớp tên có dấu/không dấu.

---

## 3. Chuyển tiền + Guardian 3 trạng thái

**Vào:** Trang chủ → "Chuyển tiền" → chọn người nhận → nhập số tiền + nội dung → "Tiếp tục".
**Chạy bằng:** **risk engine (rule, KHÔNG dùng LLM)** ở bước chấm điểm để trả dưới 300ms.
Agent chỉ vào cuộc ở **lượt 2** của màn can thiệp.

| Trạng thái | Người nhận | Số tiền | Nội dung | Kết quả |
|---|---|---|---|---|
| **pass** (điểm 9) | MAI VAN SON · VCB 3028127210 | 5.000.000 | bất kỳ | đi thẳng màn xác nhận, hiện "Đã chuyển N lần" |
| **soft_warn** (điểm 64) | NGUYEN VAN TRUNG · ACB 5270384262 | 40.000.000 | bất kỳ | banner cảnh báo ngay trên màn nhập lệnh |
| **intervene** (điểm 68) | NGUYEN VAN TRUNG · ACB 5270384262 | 85.000.000 | **`chuyen gap theo huong dan cong an`** | chèn màn Guardian 2 lượt |

**⚠️ Quan trọng cho trạng thái `intervene`:** phải có **từ khóa lừa đảo trong nội dung**
(vd "chuyen gap theo huong dan cong an"). Chỉ tăng số tiền mà nội dung trung tính thì
chỉ lên `soft_warn`.

**Màn Guardian (intervene) — 2 lượt:**
1. **Lượt 1** (hiện tức thì, rule): 3 lý do nặng nhất + câu hỏi ("có ai đang hướng dẫn bạn không").
2. **Lượt 2** (agent **Scam Shield** `01a0ba02` viết khuyến cáo, ~5–8s) + 4 nút:
   Khóa tạm 24h · Hủy · Gọi MSB 1900 6083 · Vẫn tiếp tục.

---

## 4. Scam Shield — kết luận chống lừa đảo

Agent **Scam Shield** (`01a0ba02`, `qwen3.6-flash`) gọi 4 công cụ thật
(hồ sơ khách, hồ sơ người nhận, đối chiếu playbook lừa đảo) rồi kết luận
**an toàn / cần thận trọng / nguy hiểm**.

- **Mức độ do luật quyết định (ổn định)**, agent chỉ viết lời giải thích → hỏi lại
  cùng một giao dịch luôn ra cùng một mức.
- Ví dụ: 5 triệu cho người thân (28 lần giao dịch) → **an toàn**; 85 triệu cho tài
  khoản đã bị báo cáo (SUSPECTED) + nội dung "công an" → **nguy hiểm**.

---

## 5. Ops — màn giám sát nội bộ (ngân hàng)

**Vào:** `/ops` (đăng nhập vận hành riêng, tách khỏi phiên khách).
Các màn: Dashboard KPI · Danh sách cảnh báo · Hồ sơ vụ việc · Kịch bản lừa đảo ·
Ngưỡng mô hình · **Nhật ký quyết định AI**.

- **Nhật ký quyết định AI** ghi lại **mọi lượt gọi agent** (câu hỏi, trả lời, độ trễ,
  trạng thái) — bằng chứng "AI kiểm toán được". Đã bao gồm cả Chat Banking và Scam Shield.
- ⚠️ Mật khẩu tài khoản vận hành không nằm trong repo — cần chuẩn bị trước khi demo màn này.

---

## Phụ lục — Luồng nào chạy qua AI Agent?

| Luồng | Qua Agent Platform (LLM)? | Agent · Model |
|---|---|---|
| Financial Copilot (mục 1) | ✅ Có | MSB Copilot · `glm-5.2` |
| Chat Banking (mục 2) | ✅ Có | Chat Banking · `qwen3.6-flash` |
| Guardian — chấm điểm 3 mức (mục 3) | ❌ Không (rule engine, <300ms) | — |
| Guardian — khuyến cáo lượt 2 (mục 3) | ✅ Có | Scam Shield · `qwen3.6-flash` |
| Scam Shield verdict (mục 4) | ✅ Có | Scam Shield · `qwen3.6-flash` |

**Lưu ý vận hành khi demo:**
- Nếu Copilot/Chat Banking trả lời trống hoặc "chưa xem được dữ liệu", kiểm tra
  **provider LLM** trên Console Agent trước tiên — provider Gemini từng hết hạn mức
  làm agent "chết" trong khi gateway vẫn 200.
- Một lượt Copilot mất ~20–40s; Chat Banking ~2s; Scam Shield ~5–10s. Nên demo Copilot
  với câu hỏi chuẩn bị sẵn để không phải chờ lâu trên sân khấu.
