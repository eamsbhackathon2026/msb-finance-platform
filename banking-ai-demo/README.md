# MSB AI Financial Guardian — banking-ai-demo

Sáu microservice FastAPI cung cấp API cho agent, phục vụ hai luồng nghiệp vụ:

- **Journey A — Quản lý tài chính cá nhân.** Đọc toàn cảnh tài chính, phân tích chi
  tiêu theo tháng, dự báo dòng tiền, gợi ý sản phẩm tích lũy.
- **Journey B — Phòng chống rủi ro thanh toán.** Chấm điểm rủi ro từng lệnh chuyển
  tiền trước khi tiền rời tài khoản, đối chiếu playbook lừa đảo, hỏi khách, ghi nhận
  hành động và đưa phản hồi trở lại baseline.

Tất cả service dùng PostgreSQL thật theo schema 18 bảng trong
[`../msb_guardian_schema.sql`](../msb_guardian_schema.sql).

## Nguyên tắc thiết kế

**Điểm rủi ro do engine tất định quyết định, không do LLM.** Sáu yếu tố cộng điểm
theo quy tắc cố định; LLM chỉ được viết phần diễn giải, đặt câu hỏi và làm mượt lời
khuyên. Nhờ vậy cùng một giao dịch luôn cho ra cùng một điểm, và mọi quyết định đều
giải thích được bằng con số cụ thể.

**Mọi lời khuyên đưa cho khách đều có gốc trong database.** Câu hỏi và khuyến cáo lấy
từ bảng `scam_scenario`; LLM không được tự nghĩ ra kịch bản mới hay đổi hành động
khuyến nghị. Đây là hàng rào chặn việc một câu trả lời sai của LLM đi thẳng tới khách.

**Không endpoint nào trả PII.** Họ tên đầy đủ, số giấy tờ, số điện thoại, email, địa
chỉ và ngày sinh bị loại khỏi mọi response; chỉ bản đã che được trả ra. Hai cột kiểm
thử `is_fraud` và `fraud_case_id` cũng bị loại. Response an toàn để đưa thẳng vào
prompt LLM.

**Lỗi phải nói được điều gì đó cho người gọi.** Trợ lý đọc nguyên văn thân phản hồi
của tool để quyết bước tiếp theo, nên `500 Internal Server Error` không cho nó biết
là nên tra lại id hay nên dừng. Mọi service đăng ký `install_db_error_handlers(app)`
từ `common.py`, dịch lỗi Postgres thành mã trạng thái đọc được:

| Lỗi | Trả về | Ví dụ `detail` |
|---|---|---|
| Khóa ngoại thiếu bản ghi cha (`23503`) | 404 | `customer 999999 không tồn tại` |
| Còn bản ghi con tham chiếu (`23503`) | 409 | `không xóa được customer 100008 vì còn bản ghi transaction_history tham chiếu` |
| Ghi trùng khóa duy nhất (`23505`) | 409 | `guardian_case với decision_id ... đã tồn tại` |
| Thiếu cột bắt buộc (`23502`) | 422 | `thiếu giá trị bắt buộc cho customer_id` |
| Sai ràng buộc `CHECK` (`23514`) | 422 | `giá trị không hợp lệ theo ràng buộc ck_user_role` |
| Sai định dạng, gồm uuid hỏng (`22xxx`) | 422 | `giá trị không đúng định dạng: invalid input syntax for type uuid` |
| Database không dùng được lúc này (`08xxx` mất kết nối, `53xxx` hết tài nguyên, `57P01/02/03` đang tắt hoặc khởi động) | 503 | `cơ sở dữ liệu tạm thời không truy cập được` |

SQL sai cú pháp thì **vẫn là 500**: đó là bug của service, che đi thì không ai biết mà
sửa. Giá trị trong thông điệp chỉ được nêu khi cột là cột id — cột khác chỉ nêu tên,
vì thứ Postgres trả về là dữ liệu người gọi gửi lên và có thể là PII.

**Mỗi service chỉ chạm bảng của chính nó.** Dữ liệu thuộc service khác lấy qua HTTP,
không truy vấn chéo database.

Có đúng một ngoại lệ: **kiểm tra tồn tại trước khi ghi một khóa ngoại**. Trước khi
chèn `risk_decision`, `guardian_case` hay `transaction_history`, service tra thẳng
bảng cha (`customer`, `account`, `risk_decision`, `beneficiary`, `scam_scenario`)
bằng một câu `SELECT 1`, rồi trả 404 nêu đúng thực thể. Hai lý do: khóa ngoại trong
schema vốn đã ràng hai bảng với nhau nên sự phụ thuộc là có sẵn, còn hỏi qua HTTP thì
lúc service kia chết sẽ không phân biệt được "không tồn tại" với "không gọi được" —
và trả 404 sai sự thật còn tệ hơn cái 500 mà ta đang muốn bỏ. Ngoại lệ này chỉ dành
cho câu `SELECT 1` kiểm tra tồn tại; đọc dữ liệu nghiệp vụ của service khác vẫn phải
đi qua HTTP.

## Sáu service

| Service | Bảng sở hữu | Vai trò |
|---|---|---|
| `customer-profile-service` | `customer`, `account`, `deposit`, `loan`, `beneficiary`, `behavior_profile`, `account_event` | Khách hàng là ai và bình thường họ hành xử ra sao |
| `transaction-service` | `transaction_history`, `spending_insight`, `product`, `product_recommendation` | Lịch sử giao dịch, phân tích chi tiêu, gợi ý sản phẩm |
| `risk-scoring-service` | `risk_decision`, view `ops_summary`, `ops_decision_log` | Engine 6 yếu tố và vòng đời quyết định |
| `scam-knowledge-service` | `scam_scenario`, `fraud_case` | Playbook lừa đảo và bộ kiểm thử engine |
| `action-feedback-service` | `guardian_case`, `feedback`, `notification`, `llm_trace` | Case, phản hồi closed-loop, thông báo, nhật ký LLM |
| `identity-service` | `app_role`, `app_user` | Ai được vào hệ thống, vai trò nào, quyền gì |

Mỗi service đều có:

| Đường dẫn | Nội dung |
|---|---|
| `GET /docs` | **Swagger UI** — có mô tả cho từng nhóm endpoint, thử được trực tiếp |
| `GET /redoc` | Tài liệu dạng đọc |
| `GET /openapi.json` | Đặc tả OpenAPI 3.1 |
| `GET /agent/tools` | Manifest cho agent: tên tool, mô tả, tham số, kiểu trả về |
| `GET /health` | Kiểm tra sống — **không chạm database**, để sự cố DB không làm Kubernetes khởi động lại pod |
| `GET /health/db` | Kiểm tra kết nối database |
| `GET /info` | Mô tả service và danh sách bảng sở hữu |
| `GET /` | Trang chỉ đường tới các mục trên |

## Engine rủi ro

Sáu yếu tố, mỗi yếu tố có trần điểm riêng; tổng bị chặn ở 100.

| Yếu tố | Trần | Nội dung |
|---|---:|---|
| `amount_deviation` | 25 | Số tiền lệch bao xa so với p90/p99/mức cao nhất từng chuyển, và có đang vét sạch tài khoản không |
| `new_beneficiary` | 20 | Người nhận hoàn toàn mới, mới quen, hay đã quen lâu |
| `time_of_day` | 10 | Giao dịch ban đêm — chỉ tính là bất thường với người vốn không giao dịch đêm |
| `behavior_drift` | 20 | Cờ chiếm quyền thiết bị, tổng dồn về một người nhận, chuỗi giao dịch tăng dần |
| `relationship_history` | 15 | Quan hệ với người nhận, tiền mồi, trạng thái nghi ngờ |
| `recent_context` | 20 | Sự kiện trong **60 phút trước giao dịch**: tất toán sổ, nâng hạn mức, đăng nhập thiết bị mới, đổi mật khẩu, nhận tiền lạ |

Phân mức: `pass` dưới 40 · `soft_warn` 40–74 · `intervene` từ 75.

Sáu yếu tố chỉ nhìn hành vi, không đọc khách đang chuyển tiền để làm gì. Vì vậy có
thêm một cơ chế **nâng mức**: khi nội dung chuyển khoản khớp một kịch bản trong
playbook bằng bằng chứng từ khóa, *và* playbook xếp kịch bản đó vào loại phải dừng
giao dịch (`cancel`/`hold`), thì `soft_warn` được nâng lên `intervene`. Cơ chế này
không cộng điểm (nên kẻ gian đổi nội dung chuyển khoản không kéo được điểm xuống),
không bao giờ nâng từ `pass`, và luôn được ghi lại trong `factors.scenario_escalation`
để truy vết.

### Vòng đời một lệnh chuyển tiền

Một lệnh chuyển = một dòng `risk_decision`, cả ba bước ghi lên cùng dòng đó:

```
POST /transfer/precheck    → chấm điểm, khớp kịch bản, trả câu hỏi nếu cần
POST /transfer/intervene   → ghi câu trả lời của khách, trả khuyến cáo
POST /transfer/action      → chốt hành động, mở case nếu khóa tạm hoặc gọi lại
```

Sau đó vòng phản hồi khép lại: đóng case bằng `CLOSED_FRAUD`/`CLOSED_LEGIT` tự sinh
nhãn, `POST /feedback/apply` áp nhãn đó vào baseline và trạng thái người nhận.

## Dữ liệu mẫu

`db/seed.sql` sinh từ `db/generate_seed.py` (tất định — chạy lại cho ra đúng cùng bộ
dữ liệu). Nội dung:

- **10 khách hàng** phủ đủ ba phân khúc T24: 4 SALARY (`target=1`), 3 HNW (`target=2`),
  3 SENIOR (`target=3`). Người từ 60 tuổi luôn được xếp SENIOR bất kể `target`.
- **~2.700 giao dịch** trải 6 tháng gần nhất, với chữ ký chi tiêu khác nhau theo phân
  khúc: người đi làm nhiều giao dịch nhỏ, khách ưu tiên ít giao dịch nhưng giá trị
  lớn, người cao tuổi rất ít giao dịch quanh vài nhóm quen thuộc.
- **106 người nhận**, tài khoản, sổ tiết kiệm, khoản vay, và `behavior_profile` tính
  từ chính các giao dịch sạch trong 90 ngày.
- **10 kịch bản lừa đảo** (S01–S10) theo 5 nhóm: mạo danh, deepfake, đầu tư/việc làm,
  mua bán, chiếm thiết bị.
- **10 fraud case** (F01–F10) làm bộ kiểm thử engine, kèm quyết định rủi ro, case,
  phản hồi, thông báo và nhật ký LLM.

**Cặp F01/F02 là bằng chứng quan trọng nhất khi pitch.** Cùng một khách hàng cao tuổi,
cùng chuyển một khoản lớn bất thường so với thói quen:

| | F01 | F02 |
|---|---|---|
| Số tiền | 560.000.000 | 60.000.000 |
| Người nhận | Hoàn toàn mới, quan hệ chưa xác định | Con gái, đã chuyển 28 lần |
| Bối cảnh | Tất toán sổ tiết kiệm 28 phút trước | Không có gì bất thường |
| Engine chấm | **93 → intervene** | **29 → pass** |

Chặn nhầm một giao dịch hợp lệ là lỗi tệ hơn bỏ lọt một vụ lừa đảo, nên F02 phải luôn
đi qua được.

## Chạy local

Cần Python 3.12+ và Docker.

```bash
# 1. Dựng Postgres + nạp schema + nạp dữ liệu mẫu
make dev-up
export DATABASE_URL='postgresql://postgres:guardian@localhost:5432/guardian'

# 2. Chạy 1 service
make run SERVICE=risk-scoring-service PORT=8083
# → http://localhost:8083/docs
```

Chạy cả 5 service (mỗi lệnh một cửa sổ terminal):

```bash
export DATABASE_URL='postgresql://postgres:guardian@localhost:5432/guardian'
export CUSTOMER_PROFILE_SERVICE_URL=http://localhost:8081
export TRANSACTION_SERVICE_URL=http://localhost:8082
export RISK_SCORING_SERVICE_URL=http://localhost:8083
export SCAM_KNOWLEDGE_SERVICE_URL=http://localhost:8084
export ACTION_FEEDBACK_SERVICE_URL=http://localhost:8085

make run SERVICE=customer-profile-service PORT=8081
make run SERVICE=transaction-service      PORT=8082
make run SERVICE=risk-scoring-service     PORT=8083
make run SERVICE=scam-knowledge-service   PORT=8084
make run SERVICE=action-feedback-service  PORT=8085
```

### Thử nhanh

```bash
# Journey A — chi tiêu theo tháng
curl -s localhost:8082/transactions/100001/monthly-summary | jq

# Journey A — toàn cảnh tài chính
curl -s localhost:8081/customers/100007/portfolio | jq .summary

# Journey B — chấm một lệnh chuyển tiền
curl -s -X POST localhost:8083/transfer/precheck \
  -H 'Content-Type: application/json' \
  -d '{"customer_id":100008,"account_id":5011,"amount":560000000,
       "beneficiary_bank_code":"ACB","beneficiary_account_no":"5270384262",
       "memo":"CHUYEN TIEN THEO YEU CAU CO QUAN DIEU TRA",
       "session_flags":{"on_call":true}}' | jq

# Manifest cho agent
curl -s localhost:8083/agent/tools | jq '.tools[].name'
```

## Kiểm thử

```bash
make test        # pytest cho cả 5 service — không cần database
make db-test     # chấm engine trên 10 fraud case — cần service đang chạy
```

`make db-test` in ra bảng điểm từng case và ghi kết quả vào `fraud_case.last_test_score`.
**Chạy trước mỗi lần demo.** Nếu F02 bị chặn thì hệ thống đang cảnh báo sai — lỗi này
nghiêm trọng hơn việc một case gian lận chỉ đạt `soft_warn`.

## Nạp database khi không có psql

`make` tự dùng `psql` nếu máy có sẵn; nếu không thì chạy qua container
`postgres:16-alpine`, nên chỉ cần Docker là đủ.

```bash
export DATABASE_URL='postgresql://anhnv20:PASSWORD@HOST:5432/ea-hackathon?sslmode=require'

make db-ping      # thử kết nối — chạy đầu tiên
make db-status    # xem đã có schema và dữ liệu chưa
make db-reset     # nạp schema + 10 khách hàng
```

## Cấu hình

Các service đọc thẳng từ biến môi trường, không có file cấu hình nào khác. Xem
[`.env.example`](.env.example) cho danh sách đầy đủ.

| Biến | Bắt buộc | Nội dung |
|---|:---:|---|
| `DATABASE_URL` | ✓ | Chuỗi kết nối PostgreSQL. Hoặc dùng bộ `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` |
| `*_SERVICE_URL` | ✓ | Địa chỉ 4 service còn lại. **Bắt buộc khi mỗi service deploy một nơi** (GreenNode); trong cùng namespace Kubernetes thì tên service là đủ |
| `PEER_TIMEOUT_SECONDS` | | Thời gian chờ khi gọi service khác, mặc định 5 |
| `DB_POOL_MIN` / `DB_POOL_MAX` | | Kích thước pool kết nối, mặc định 1/5 |
| `PUBLIC_BASE_URL` | | Địa chỉ công khai của chính service, để nút "Try it out" trên Swagger gọi đúng chỗ khi đứng sau ingress |

Khi một service không gọi được service khác, endpoint chính **vẫn trả kết quả** trên
phần dữ liệu lấy được và đánh dấu trong trường `degraded`, thay vì trả lỗi. Thà cảnh
báo thiếu ngữ cảnh còn hơn để một giao dịch đi qua mà không ai chấm.

## Cấu trúc repo

`.github/workflows/` nằm ở **gốc git repo** (cấp trên `banking-ai-demo/`) để GitHub
Actions đọc được.

```
<repo-root>/
  msb_guardian_schema.sql        schema 18 bảng
  .github/workflows/             1 workflow / service: test → build → deploy
  banking-ai-demo/
    services/
      _common/common.py          bản gốc của lớp dùng chung
      <tên>-service/
        main.py  common.py  requirements.txt  Dockerfile  tests/
    db/
      generate_seed.py           sinh dữ liệu mẫu (tất định)
      seed.sql                   kết quả đã sinh sẵn
      run_fraud_tests.py         chấm engine trên 10 fraud case
    k8s/
      namespace.yaml  config.yaml  db-secret.example.yaml
      <tên>.yaml                 Deployment + Service từng service
    Makefile  .env.example  README.md
```

### Về `common.py`

Docker build context là `services/<tên>` nên mỗi service phải có bản copy riêng của
`common.py`. Bản gốc duy nhất được sửa là `services/_common/common.py`; sau khi sửa
phải chạy:

```bash
make sync-common
```

CI có bước kiểm tra bản copy chưa bị lệch — nếu quên đồng bộ, build sẽ báo lỗi thay
vì âm thầm deploy code cũ.

## Triển khai

### Kubernetes / VKS

```bash
export KUBECONFIG=~/.kube/vks-finance-demo.yaml
export DATABASE_URL='postgresql://anhnv20:PASSWORD@DB_HOST:5432/ea-hackathon?sslmode=require'

make deploy-prep    # ping DB → nạp schema+seed → tạo secret → kiểm tra từ trong cluster
make redeploy       # deploy image của commit hiện tại, rollout, rồi tự xác minh
```

`deploy-prep` dừng ngay ở bước đầu tiên hỏng, nên bạn biết chính xác vấn đề nằm ở
đâu thay vì phải đoán từ một pod `CrashLoopBackOff`.

Bước `k8s-db-check` đáng chú ý: nó thử kết nối database **từ bên trong namespace**.
Laptop và cluster đi ra Internet bằng hai địa chỉ khác nhau, mà PostgreSQL trên VNG
Cloud thường lọc theo IP hoặc VPC — nên "máy tôi kết nối được" không chứng minh được
pod cũng kết nối được. Đây là nguyên nhân hay gặp khi pod lên xanh nhưng mọi endpoint
nghiệp vụ đều lỗi.

`redeploy` gắn thẳng commit SHA làm tag image, không dùng `:latest`. Manifest để
`imagePullPolicy: IfNotPresent`, nên nếu deploy bằng `:latest` thì node đã cache bản
cũ sẽ dùng lại nó — pod khởi động lại nhưng vẫn chạy code cũ, và nhìn bên ngoài mọi
thứ đều xanh.

### Truy cập sau khi deploy

Cả 5 Service đều là `ClusterIP`, **cố ý không phơi ra Internet**: API chưa có xác
thực, mà trong đó có những endpoint ghi như `POST /transfer/action` (khóa hoặc hủy
giao dịch) và `POST /feedback/apply` (ghi đè baseline hành vi). Đường vào là
port-forward:

```bash
export KUBECONFIG=~/.kube/vks-finance-demo.yaml
make port-forward
```

```
  customer-profile-service   http://localhost:8081/docs   db=ok
  transaction-service        http://localhost:8082/docs   db=ok
  risk-scoring-service       http://localhost:8083/docs   db=ok
  scam-knowledge-service     http://localhost:8084/docs   db=ok
  action-feedback-service    http://localhost:8085/docs   db=ok
```

Cổng trùng với quy ước chạy local, nên cùng một lệnh `curl` dùng được ở cả hai nơi.

```bash
make k8s-verify     # image đang chạy + kết nối DB của từng service
```

Nếu sau này cần agent gọi từ ngoài cluster, đổi `spec.type` sang `LoadBalancer`
(cluster đã có sẵn `vngcloud-load-balancer-controller`) — nhưng hãy bổ sung xác thực
trước, đừng mở trần.

Manifest tham chiếu hai đối tượng phải tồn tại trước:

- Secret `guardian-db` chứa `DATABASE_URL` — tạo bằng `make k8s-db-secret`, **không
  bao giờ commit vào repo**
- ConfigMap `guardian-endpoints` chứa địa chỉ các service — `k8s/config.yaml`

### GreenNode (mỗi service một nơi)

Các service không phụ thuộc vào DNS nội bộ của Kubernetes. Với mỗi service, đặt:

- `DATABASE_URL` trỏ tới PostgreSQL dùng chung
- Bốn biến `*_SERVICE_URL` trỏ tới URL công khai của các service còn lại
- `PUBLIC_BASE_URL` trỏ tới chính nó, để Swagger "Try it out" gọi đúng địa chỉ

### CI/CD

Mỗi service có một workflow tại `<repo-root>/.github/workflows/<tên>.yml`, chạy khi
thư mục service đó **hoặc** `services/_common/` thay đổi. Ba job nối tiếp:

1. **`test`** — kiểm tra `common.py` chưa lệch khỏi bản gốc, rồi chạy pytest
2. **`build-and-push`** — build image, push lên vCR với hai tag `:<commit SHA>` và `:latest`
3. **`deploy`** — apply ConfigMap, kiểm tra secret `guardian-db` tồn tại, rồi rollout

Credentials đặt trong GitHub Secrets:

| Secret | Nội dung |
|---|---|
| `VCR_USERNAME` | Username đăng nhập vCR |
| `VCR_PASSWORD` | Password vCR |
| `KUBE_CONFIG` | Kubeconfig VKS đã base64 |

```bash
base64 -i ~/.kube/vks-finance-demo.yaml | pbcopy      # macOS
gh secret set KUBE_CONFIG < <(base64 -w0 ~/.kube/vks-finance-demo.yaml)
```

## identity-service — danh tính và phân quyền

Sở hữu `app_role` và `app_user`. Hai bảng này được tạo trên database sau 18 bảng
nghiệp vụ, nên phần khai báo trong `msb_guardian_schema.sql` chép lại đúng cấu
trúc đang có chứ không phải bản thiết kế ban đầu.

Dữ liệu hiện tại: 55 tài khoản — 5 nội bộ (`admin`, `ops.analyst1`,
`ops.analyst2`, `ops.manager`, `auditor`) và 50 tài khoản khách hàng gắn với
`customer_id`. Mật khẩu lưu dạng bcrypt.

### Ba hàng rào dữ liệu

**`password_hash` không bao giờ rời khỏi service này.** Không endpoint nào trả
ra nó, kể cả đã che. Nó chỉ được đọc trong bộ nhớ khi so khớp ở
`POST /auth/verify`. Hàm che dữ liệu dựng dict mới theo danh sách trắng thay vì
xoá khoá khỏi dict cũ, nên thêm cột vào bảng thì cột đó **không** tự lọt ra
response — có test riêng cho điều này.

**Mọi PII đều bị che** — họ tên, email, số điện thoại. Tên trường ghi rõ hậu tố
`_masked` để bên gọi không nhầm là giá trị thật.

**Chỉ chạm hai bảng của chính mình.** Thông tin khách hàng lấy qua HTTP tới
`customer-profile-service`.

### `app_role` đang trống — và service nói rõ điều đó

`app_user.role` đã có `ADMIN` và `CUSTOMER`, nhưng `app_role` chưa có dòng nào.
`GET /users/{id}/permissions` vì vậy trả cờ `role_defined: false` kèm ghi chú,
**không** trả mảng rỗng. Hai thứ này khác hẳn nhau: "chưa khai báo" mà bị hiểu
thành "không có quyền nào" sẽ dẫn tới chặn nhầm người dùng hợp lệ.

`GET /roles` cũng liệt kê `undeclared_roles_in_use` — các vai trò đang được dùng
mà chưa khai báo — thay vì để người đọc tự đối chiếu hai danh sách.

Khai báo vai trò bằng `PUT /roles/{role_code}`, idempotent nên gọi lại không tạo
bản ghi thứ hai.

### Giá trị hợp lệ lấy từ ràng buộc của database

Database có sẵn các `CHECK` constraint; service khai báo lại đúng chúng trong
kiểu dữ liệu để request sai nhận **422 kèm danh sách giá trị đúng**, thay vì
`CheckViolation` biến thành `500 Internal Server Error` không đọc được.

| Ràng buộc | Giá trị hợp lệ |
|---|---|
| `ck_role_scope` | `APP`, `BACKOFFICE` |
| `ck_role_status` | `ACTIVE`, `INACTIVE` |
| `ck_user_role` | `CUSTOMER`, `ADMIN` |
| `ck_user_status` | `ACTIVE`, `LOCKED`, `DISABLED` |
| `ck_user_customer` | `CUSTOMER` phải có `customer_id`; `ADMIN` phải để trống |

`ck_user_customer` đáng chú ý: không thể tạo tài khoản nội bộ gắn với một khách
hàng cụ thể, và ngược lại.

### Khoá tài khoản

Sai mật khẩu liên tiếp `MAX_FAILED_LOGIN` lần (mặc định 5) thì trạng thái chuyển
`LOCKED`. Phân biệt với `DISABLED`: `DISABLED` là quyết định của quản trị viên,
`LOCKED` là hệ quả tự động. Mở lại bằng `PATCH /users/{id}/status` với `ACTIVE`,
và thao tác đó đặt lại bộ đếm — không đặt lại thì lần sai tiếp theo khoá ngay.

`POST /auth/verify` trả **cùng một câu trả lời** cho sai mật khẩu và không có tài
khoản. Khác biệt nhỏ nhất giữa hai trường hợp cũng đủ để dò ra danh sách người
dùng nào tồn tại.

## guardian-gateway — backend cho web app

Service thứ sáu, đứng riêng khỏi 5 service domain. Nó tồn tại để
[`msb-guardian-fe`](https://github.com/eamsbhackathon2026/msb-guardian-fe) có đúng
một địa chỉ và một hợp đồng để gọi.

Web app gọi 7 đường dẫn cùng origin dưới `/api/*`, đã gộp sẵn theo màn hình. 5
service domain thì phơi ra 74 endpoint ở mức chi tiết hơn hẳn — ví dụ màn
Financial Copilot cần gộp `portfolio` + `insights` + `cashflow-forecast` +
`recommendations` từ 2 service khác nhau. Gộp ở server giữ cho trình duyệt không
phải biết 5 địa chỉ và không phải tự nắn dữ liệu.

| Endpoint | Màn hình dùng |
|---|---|
| `GET /api/copilot/overview` | Financial Copilot — Tổng quan |
| `POST /api/copilot/chat` | Chat AI (Server-Sent Events) |
| `POST /api/risk/assess` | Scam Shield — cảnh báo chặn giao dịch |
| `GET /api/ops/metrics` | Ops Dashboard — 4 chỉ số KPI |
| `GET /api/ops/alerts` | Ops Dashboard — danh sách cảnh báo |
| `GET /api/ops/alerts/{id}` | Chi tiết case |
| `POST /api/ops/alerts/{id}/decision` | Ghi quyết định xử lý |

Kiểu dữ liệu trả về là bản dịch 1-1 của `src/data/types.ts` bên FE, đặt trong
`services/guardian-gateway/models.py`.

### Gateway đọc dữ liệu thật từ 5 service domain

`domain.py` gọi sang 5 service, `mappers.py` dịch sang hợp đồng FE. Chia hai file
vì hai việc khác nhau: gọi mạng có thể hỏng, còn dịch dữ liệu là hàm thuần và
kiểm tra được mà không cần service nào chạy.

| Endpoint FE | Lấy từ |
|---|---|
| `/api/session/customer` | `customer-profile` `/customers/{id}` + `/portfolio` |
| `/api/home` | `/customers/{id}` + `transaction` `/insights` |
| `/api/copilot/overview` | `transaction` `/transactions/{id}/monthly-summary` + `/insights` |
| `/api/risk/assess` | `risk-scoring` `POST /transfer/precheck` — **engine 6 yếu tố thật** |
| `/api/risk/explain` | precheck + `scam-knowledge` `/scams/{id}` + `customer-profile` `/events` |
| `/api/transfer/pending` | `scam-knowledge` `/fraud-cases/{id}` |
| `/api/transfer/action` | `risk-scoring` `POST /transfer/action` |
| `/api/safety-center` | `action-feedback` `/cases` |
| `/api/ops/*` | `risk-scoring` `/risk-decisions`, `/ops/summary`, `/ops/decisions` |
| `/api/ops/alerts/{id}/decision` | `risk-scoring` `POST /transfer/action` |

`catalog.py` vẫn còn và trở thành **đường lui**: service nào hỏng thì phần dữ
liệu đó lấy từ đây thay vì để màn hình trắng. 5 service deploy độc lập nên một
cái chết không được kéo theo cả web app.

### Ba giá trị được SUY RA, không lấy thẳng từ database

Ghi rõ ở đây để không ai nhầm chúng với số liệu thật:

**Ngân sách tháng** — domain không có khái niệm ngân sách. Lấy 120% chi tiêu.
Từng thử dùng thu nhập của kỳ, nhưng thu nhập gồm cả tiền tất toán sổ tiết kiệm:
tháng 09 ra ngân sách 527 triệu trong khi chi tiêu thật 2,4 triệu.

**Chuyển khoản bị loại khỏi phân tích chi tiêu** — chuyển tiền đi không phải tiêu
dùng. Để nguyên thì một lệnh 620 triệu chiếm 100% biểu đồ và các nhóm thật bị ép
về 0%.

**Số tài khoản hiển thị** dùng `account_id` làm phần đuôi. Không endpoint nào của
domain trả số tài khoản thật — đó là chủ ý, không phải thiếu sót.

### Luồng agent — đã dựng xong, chờ một API key LLM

Thiết lập trong `hackathon-agent-platform` được tạo **hoàn toàn qua API công
khai của nó**, không sửa một dòng code nào trong repo đó.

| Thành phần | Trạng thái |
|---|---|
| Tài khoản owner | ✅ `owner@msb-guardian.local` (mật khẩu trong `deploy/.env.production` của repo agent) |
| Kết nối API → gateway | ✅ `http://guardian-gateway.finance-demo.svc.cluster.local` |
| Tool `quarterly-spending` | ✅ `GET /api/copilot/quarters` |
| Tool `monthly-overview` | ✅ `GET /api/copilot/overview` |
| Tool `customer-profile` | ✅ `GET /api/session/customer` |
| API key cho gateway | ✅ Secret `guardian-agent` trong `finance-demo` |
| Provider LLM | ❌ **chưa có** — cần API key của GreenNode, Gemini hoặc endpoint tương thích OpenAI |
| Agent | ❌ chặn bởi provider: `AgentCreateRequest` bắt buộc `provider_id` |

**Đã kiểm chứng nền tảng agent gọi được gateway.** Chạy thử tool
`quarterly-spending` trả `ok: true`, HTTP 200, đúng dữ liệu quý thật — nghĩa là
hàng rào egress cho qua và DNS cross-namespace hoạt động.

Còn đúng ba bước, làm trong giao diện admin-web hoặc bằng API:

```
1. Tạo provider   POST /v1/providers   (kind: greennode | gemini | openai_compatible)
2. Tạo agent      POST /v1/agents      (provider_id, model, gắn 3 tool ở trên)
3. Điền AGENT_ID  vào k8s/guardian-gateway.yaml rồi push
```

`AGENT_ID` để trống là **có chủ đích**: gateway coi agent là chưa cấu hình và
dùng kịch bản trả lời có sẵn, nên chat vẫn chạy bình thường trong lúc chờ.

### agent-service — chỉ gọi, không sửa

Chat hỏi `agent-service` khi có đủ `AGENT_SERVICE_URL`, `AGENT_API_KEY`,
`AGENT_ID`; thiếu bất kỳ cái nào hoặc gọi hỏng thì dùng kịch bản trả lời có sẵn.
Nền tảng agent có xác thực riêng và cần một agent tạo sẵn bên trong nó — phần
thiết lập ấy **thuộc repo khác**, gateway không tự làm và không sửa gì ở đó.

Service nằm khác namespace nên địa chỉ nội bộ là
`http://agent-service.agent-platform.svc.cluster.local:8080`.

### Header `X-Guardian-Data-Source` cho biết dữ liệu đến từ đâu

`guardedCall` trong `src/lib/demo-mode.ts` bắt mọi lỗi và timeout 6s rồi **âm
thầm** rơi về dữ liệu demo. Gateway chết hẳn thì màn hình vẫn đẹp như thường —
nhìn giao diện không thể biết FE đang gọi được backend hay không.

Mỗi response mang header này với một trong ba giá trị, tính theo **từng request**:

| Giá trị | Nghĩa |
|---|---|
| `domain` | mọi lời gọi sang service domain đều thành công |
| `degraded` | có ít nhất một lời gọi hỏng, phần đó lấy từ `catalog.py` |
| `stub` | không gọi domain lần nào (`DOMAIN_ENABLED=false`, hoặc endpoint tĩnh) |

Mở tab Network là biết ngay, không phải đoán. `degraded` nghĩa là màn hình vẫn
đẹp nhưng một phần số liệu không phải dữ liệu thật — đúng thứ cần nhìn thấy
trước khi trình bày.

### Biến môi trường

| Biến | Mặc định | Nội dung |
|---|---|---|
| `DOMAIN_ENABLED` | `true` | `false` để chạy hoàn toàn bằng `catalog.py` |
| `DEMO_CUSTOMER_ID` | `100008` | Khách hàng của kịch bản demo |
| `DEMO_FRAUD_CASE_ID` | `F01` | Fraud case cung cấp số tiền và nội dung chuyển khoản |
| `RISK_ASSESS_DELAY_MS` | `800` | Độ trễ cố ý thêm vào `/api/risk/assess` |
| `PEER_TIMEOUT_SECONDS` | `5` | Thời gian chờ mỗi lời gọi sang domain |
| `AGENT_SERVICE_URL` / `AGENT_API_KEY` / `AGENT_ID` | rỗng | Thiếu thì chat dùng kịch bản có sẵn |

`RISK_ASSESS_DELAY_MS` cần giải thích: màn "Đang phân tích giao dịch..." bên FE
hiển thị theo `isPending` của react-query, tức là nó dài **đúng bằng** thời gian
chờ API. Demo mode giả lập 1.800–2.200ms; gateway trả tức thì sẽ làm màn đó loé
qua rồi biến mất. Giữ 2000 để chuyển demo→live không đổi nhịp trình bày; đặt `0`
nếu muốn đo tốc độ thật.

### Chạy local

```bash
cd services/guardian-gateway
pip install -r requirements.txt
uvicorn main:app --port 8000        # cổng 8000 đúng như proxy trong vite.config.ts
python -m pytest -q                 # 29 test, không cần database
```

Sau đó chạy FE với `npm run dev` rồi mở `http://localhost:5173/?demo=0&debug=1`.

### Port-forward sau khi deploy

Gateway **không** nằm trong biến `SERVICES` của Makefile — `make sync-common`,
`make build` và `make port-forward` chỉ áp cho 5 service domain, vì gateway
không dùng `common.py` và không chạm database. Mở riêng:

```bash
kubectl -n finance-demo port-forward svc/guardian-gateway 8086:80
```

## Giới hạn hiện tại

- Chưa có xác thực / phân quyền trên API. Trong phạm vi hackathon các service chạy
  trong mạng tin cậy; trước khi đưa ra ngoài phải bổ sung.
- `notification` là kênh mock, chỉ ghi vào database chứ chưa thực sự gửi đi.
- Chưa tích hợp LLM. Các trường `llm_reasons`, `advice_body`, `insight_text` nhận nội
  dung do bên gọi truyền vào; `llm_trace` đã sẵn sàng để ghi nhật ký mọi lượt gọi.
- `fraud_case` chứa đáp án kỳ vọng của bộ kiểm thử nên **không được expose** ra ứng
  dụng khách hàng.
- `guardian-gateway` mới dựng xong hợp đồng với web app, chưa gọi sang 5 service
  domain — số liệu nó trả về đến từ `catalog.py`.
- Bên FE còn hai chỗ chưa chạy live kể cả khi gateway đã sẵn sàng: `streamChat`
  không nhận `chart` ở nhánh live, và `getCaseTimeline` chưa có nhánh gọi mạng
  nào (cả hai trong `src/lib/api.ts`).
