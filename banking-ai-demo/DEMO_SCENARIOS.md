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

## ⚡ Tra nhanh — gõ câu nào, ở đâu

Hai nơi chat: **Copilot** = Trợ lý AI (`/copilot/chat`, tư vấn tài chính) · **Chat Banking** = chatpay (`/chat-banking`, chuyển tiền bằng câu nói).

| Gõ câu này | Vào | Ra gì (tóm tắt) |
|---|---|---|
| `tháng này tôi tiêu nhiều nhất vào đâu` | Copilot | bảng chi tiêu tháng |
| `giúp tôi tiết kiệm 500 triệu trong 3 năm tới` | Copilot | lộ trình + 4 bảng, nói thẳng không đạt |
| `tình hình tài chính của tôi có ổn không` | Copilot | điểm sức khỏe 58/Khá, 5 trụ cột |
| `nếu tôi mất thu nhập vài tháng thì trụ được bao lâu` | Copilot | trụ ~10 tháng — đủ sức |
| `lỡ tôi ốm phải lo gấp 50 triệu thì có trụ được không` | Copilot | thiếu 21,3tr — rủi ro |
| `tôi có tiền để không trong tài khoản, nên gửi tiết kiệm thế nào` | Copilot | giữ 16,7tr + gửi 11,957tr, lãi 705k/năm |
| `tôi muốn vay 60 triệu trả trong 12 tháng, có kham nổi không` | Copilot | quá sức (69% thu nhập), an toàn ~34,8tr |
| `giúp tôi lập ngân sách chi tiêu hàng tháng` | Copilot | bảng 3 nhóm, tiết kiệm 13% vs mục tiêu 20% |
| `chuyển cho anh Sơn 2 triệu rưỡi` | Chat Banking | soạn lệnh 2,5tr → MAI VAN SON (pass) |
| `chuyển cho Trung 85 triệu, công an bảo chuyển gấp để chứng minh trong sạch` | Chat Banking | 🔴 Guardian chặn — SC-01 công an |
| `chuyển 3 triệu đóng phí nhận quà trúng thưởng xổ số` | Chat Banking | 🔴 Guardian chặn — SC-02 trúng thưởng |
| `chuyển 30 triệu nạp vào sàn đầu tư lợi nhuận cao` | Chat Banking | 🔴 Guardian chặn — SC-03 đầu tư |
| `chuyển 15 triệu gửi quà cho bạn trai nước ngoài` | Chat Banking | 🔴 Guardian chặn — SC-05 tình cảm |
| `chuyển 500 nghìn phí ship cho người giao hàng` | Chat Banking | 🟡 Banner cảnh báo — SC-04 shipper |

Chi tiết từng câu (kết quả đầy đủ, agent/tool chạy, điểm nhấn) ở các mục bên dưới.

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

### 1.6. Đánh thức tiền nhàn rỗi ⭐
- **Gửi lên:** Copilot. **Gõ:** `tôi có tiền để không trong tài khoản, nên gửi tiết kiệm thế nào`
- **Agent gọi:** `get_portfolio` → `optimize_idle_cash`.
- **Kết quả:** giữ **16.722.000 ₫** làm quỹ dự phòng (6 tháng chi thiết yếu), đem gửi
  **11.957.000 ₫** kỳ 12 tháng; gói lãi cao nhất 5,9% → nhận thêm **705.463 ₫**. Nhấn: tiền
  đang để không mỗi năm lỡ mất đúng khoản lãi đó.
- **Điểm nhấn:** biến phát hiện "tiền nhàn rỗi" ở khám sức khỏe (1.3) thành hành động cụ
  thể; giữ quỹ dự phòng trước, không khuyên khoá sạch tiền.

### 1.7. Vay bao nhiêu thì kham nổi ⭐
- **Gửi lên:** Copilot. **Gõ:** `tôi muốn vay 60 triệu trả trong 12 tháng, có kham nổi không`
- **Agent gọi:** `check_loan_affordability`.
- **Kết quả:** nói thẳng **quá sức** — trả góp ~**5.233.187 ₫/tháng** (lãi 8,5%), bằng **69%
  thu nhập**; mức vay an toàn hơn ~**34,8 triệu**, hoặc kéo dài kỳ hạn để giảm trả góp.
- **Điểm nhấn:** cố vấn có trách nhiệm — trả góp tính theo dư nợ giảm dần trên lãi thật,
  không cổ vũ khoản vay khách gánh không nổi.

### 1.8. Lập ngân sách hàng tháng
- **Gửi lên:** Copilot. **Gõ:** `giúp tôi lập ngân sách chi tiêu hàng tháng`
- **Agent gọi:** `make_budget_plan`.
- **Kết quả:** bảng 3 nhóm (Thiết yếu 2,79tr · Cam kết 3,05tr · Co giãn 0,25tr) — đang tiêu
  vs nên tiêu; đang để dành **13%**, mục tiêu **20%** thu nhập.
- **Điểm nhấn:** kê đơn cụ thể (khác chẩn đoán ở 1.3); chỉ rõ nhóm nào vượt và nên tiết kiệm thêm.

### 1.9. Câu phụ (nếu còn thời gian)
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

## 2B. Chat Banking chặn lừa đảo — 5 kịch bản ⭐

**Gửi lên:** Chat Banking (`/chat-banking`) — **gõ nguyên câu** rồi bấm "Tạo lệnh chuyển".
**Cơ chế:** câu khách gõ trở thành **nội dung chuyển khoản** → Guardian đọc từ khóa,
đối chiếu playbook lừa đảo (10 kịch bản) → chèn màn Guardian, Scam Shield khuyến cáo
đúng loại. Tất cả dùng người nhận **NGUYEN VAN TRUNG** (có sẵn trong danh bạ, tài khoản
mới + đã bị báo cáo SUSPECTED). *Điểm nhấn: khách bị lừa gõ nguyên lời kẻ gian dặn, AI
chặn ngay tại câu nói.*

| # | Câu gõ vào Chat Banking | Kết quả | Scam Shield khuyến cáo |
|---|---|---|---|
| **SC-01** Công an | `chuyển cho Trung 85 triệu, công an bảo chuyển gấp để chứng minh trong sạch` | 🔴 **Guardian chặn** (S01, 68đ) | "Công an không bao giờ yêu cầu chuyển tiền" |
| **SC-02** Trúng thưởng | `chuyển 3 triệu đóng phí nhận quà trúng thưởng xổ số` | 🔴 **Guardian chặn** (S07, 52đ) | "Giải thưởng thật không thu phí trước" |
| **SC-03** Đầu tư | `chuyển 30 triệu nạp vào sàn đầu tư lợi nhuận cao` | 🔴 **Guardian chặn** (S04, 57đ) | "Rút được lần đầu là cách họ lấy niềm tin" |
| **SC-05** Tình cảm | `chuyển 15 triệu gửi quà cho bạn trai nước ngoài` | 🔴 **Guardian chặn** (S08, 52đ) | cảnh báo lừa đảo tình cảm/gửi quà |
| **SC-04** Shipper | `chuyển 500 nghìn phí ship cho người giao hàng` | 🟡 **Banner cảnh báo** (S11, 43đ) | *(mức trung bình — banner ngay trên màn, không chèn Guardian)* |

**Lưu ý demo:**
- SC-01..SC-03, SC-05 → điểm ≥ ngưỡng nên **chèn màn Guardian** (2 lượt: lý do + khuyến cáo Scam Shield). Đúng mức "nguy hiểm cao".
- SC-04 shipper → **soft_warn** (43đ), chỉ hiện banner cảnh báo trên màn nhập lệnh, đúng mức "🟠 trung bình". Muốn cho lên màn Guardian đầy đủ thì tăng số tiền hoặc dựng kịch bản shipper nặng hơn.
- Kịch bản S11 (shipper) là kịch bản **mới thêm vào playbook** cho demo này (từ khóa: shipper, giao hàng, mã vận đơn, phí ship…).
- Sau khi bấm chọn ở màn Guardian, **lượt 2 gọi agent Scam Shield** (`01a0ba02`) viết khuyến cáo đúng loại lừa đảo — mỗi kịch bản một thông điệp khác nhau.

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

## 4B. Trung tâm an toàn — Hạn mức chi an toàn (khách tự đặt) ⭐

**Vào:** `/safety-center` → mục **Lớp bảo vệ** → **Hạn mức chi an toàn**.
Đây là thao tác UI (không phải câu chat) — khách tự đặt ngưỡng cảnh báo.

Kịch bản demo (thao tác, không gõ chat):
1. Vào Trung tâm an toàn → Lớp bảo vệ → bấm dòng **"Cảnh báo khi một giao dịch vượt …"**
   → nhập ngưỡng, ví dụ **3.000.000 ₫** → **Lưu**.
2. Vào Chuyển tiền → chọn người nhận → nhập số tiền **trên ngưỡng** (vd 5.000.000 ₫).
3. Ngay dưới ô số tiền hiện banner: *"Giao dịch này vượt **hạn mức chi an toàn** bạn đặt
   (3.000.000 ₫). Hãy kiểm tra kỹ trước khi chuyển."*

- **Điểm nhấn:** lớp bảo vệ do **chính khách chủ động** đặt, độc lập với engine Guardian —
  ngưỡng lưu ở gateway (`PATCH /api/safety-center/protections/spending_warn`), màn chuyển
  tiền đọc lại và cảnh báo tức thì client-side. Mặc định 10.000.000 ₫, đổi được bất kỳ lúc nào.

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
