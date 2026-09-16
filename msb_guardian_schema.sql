-- =====================================================================
-- MSB AI Financial Guardian · PostgreSQL DDL (chuyển từ DBML, 18 bảng)
-- Chạy: psql -U guardian -d guardian -f msb_guardian_schema.sql
--  1. Kiểu khóa ngoại được thống nhất với khóa chính: customer_id, product_id,
--     account_id, beneficiary_id, transaction_id, insight_id → INTEGER.
--  2. Bảng "case" là từ khóa SQL → đặt tên guardian_case.
--  3. Các cột mô tả là JSON trong DBML → JSONB.
--  4. Các Ref kiểu "product_group <> product.product_group", "currency <> ..."
--     không phải khóa ngoại thực → không tạo constraint, chỉ giữ CHECK/ghi chú.
--  5. Ref vòng transaction_history.risk_decision_id ↔ risk_decision.transaction_id
--     → tạo bằng ALTER TABLE ở cuối file.
--  6. Các cột giữ varchar theo core T24 (amount, rate, date...) giữ nguyên
--     varchar như DBML; service cast trước khi tính.
-- =====================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------
-- NHÓM 0 · DANH MỤC & KHÁCH HÀNG (mô phỏng T24)
-- ---------------------------------------------------------------------

CREATE TABLE product (
  product_id        INTEGER      PRIMARY KEY,
  product_name      VARCHAR(60)  NOT NULL,           -- Tên hiển thị cho khách hàng; LLM đọc cột này để diễn giải gợi ý
  product_group     VARCHAR(20)  NOT NULL,           -- SAVINGS | LOAN | CARD | INSURANCE | INVESTMENT; Copilot chỉ gợi ý SAVINGS, INVESTMENT
  product_currency  VARCHAR(3)   NOT NULL DEFAULT 'VND', -- ISO 4217; engine lọc theo currency tài khoản khách
  product_status    VARCHAR(15)  NOT NULL DEFAULT 'ACTIVE', -- ACTIVE | INACTIVE | COMING_SOON; chỉ ACTIVE được gợi ý
  product_interest  VARCHAR(50),                     -- Lãi suất dạng chuỗi hiển thị, KHÔNG dùng để tính
  CONSTRAINT ck_product_group  CHECK (product_group IN ('SAVINGS','LOAN','CARD','INSURANCE','INVESTMENT')),
  CONSTRAINT ck_product_status CHECK (product_status IN ('ACTIVE','INACTIVE','COMING_SOON'))
);
COMMENT ON TABLE product IS 'Danh mục sản phẩm MSB (giả lập) phục vụ Journey A – Financial Copilot. Dữ liệu tham chiếu, seed một lần, không cập nhật trong luồng giao dịch. Nếu cần so sánh lãi suất, thêm cột product_interest_rate NUMERIC.';

CREATE TABLE customer (
  customer_id        INTEGER      PRIMARY KEY,       -- Mã CIF (CUSTOMER ID bên T24)
  full_name          VARCHAR(120) NOT NULL,          -- PII – KHÔNG đưa vào LLM; service tạo name_masked
  street             VARCHAR(255),                   -- PII – chỉ hiển thị hồ sơ
  target             VARCHAR(5)   NOT NULL,          -- Phân khúc T24: 1 = SALARY, 2 = HNW, 3 = SENIOR (BA chốt mapping) → persona
  nationality        VARCHAR(2)   NOT NULL DEFAULT 'VN', -- ISO 3166-1 alpha-2
  legal_id           VARCHAR(20),                    -- CCCD/hộ chiếu. PII nhạy cảm – mask toàn bộ
  legal_type         VARCHAR(10),                    -- CCCD | PASSPORT | CMND
  legal_description  VARCHAR(120),                   -- Nơi cấp
  date_of_birth      VARCHAR(8),                     -- YYYYMMDD theo T24; service tính tuổi → SENIOR (>= 60)
  email              VARCHAR(120),                   -- Giả lập; chỉ dùng notification mock
  phone_no           VARCHAR(15),                    -- Số callback khi khách chọn "Khóa tạm 24h"; mask "09** *** 123" trên UI
  co_code            VARCHAR(10),                    -- Company code T24
  CONSTRAINT ck_customer_legal_type CHECK (legal_type IS NULL OR legal_type IN ('CCCD','PASSPORT','CMND'))
);
COMMENT ON TABLE customer IS 'Hồ sơ khách hàng (giả lập, mô phỏng T24). target + date_of_birth quyết định persona và baseline; phone_no là kênh callback. Các cột PII (full_name, street, legal_id, date_of_birth, email, phone_no) KHÔNG đưa vào prompt LLM và phải mask trong risk_decision/llm_trace.';

CREATE TABLE account (
  account_id       INTEGER      PRIMARY KEY,         -- Số tài khoản thanh toán (ACCOUNT ID bên T24)
  product_group    VARCHAR(20)  NOT NULL,            -- ACCOUNT_CURRENT | ACCOUNT_SALARY | ACCOUNT_BUSINESS
  product_id       INTEGER      REFERENCES product(product_id),
  customer_id      INTEGER      NOT NULL REFERENCES customer(customer_id),
  account_name     VARCHAR(60),                      -- "TK lương", "TK chi tiêu"
  currency         VARCHAR(3)   NOT NULL DEFAULT 'VND',
  open_acct_bal    VARCHAR(20),                      -- Số dư đầu ngày; Copilot dùng
  working_balance  VARCHAR(20)  NOT NULL,            -- Số dư khả dụng; precheck tính drain_ratio = amount / working_balance. NULL/0 → engine trả lỗi
  co_code          VARCHAR(10),
  CONSTRAINT ck_account_group CHECK (product_group IN ('ACCOUNT_CURRENT','ACCOUNT_SALARY','ACCOUNT_BUSINESS'))
);
CREATE INDEX ix_account_customer ON account (customer_id);
COMMENT ON TABLE account IS 'Tài khoản thanh toán (giả lập, mô phỏng T24). Tài khoản nguồn của mọi lệnh chuyển tiền (Journey B) và nơi Copilot đọc số dư (Journey A). open_acct_bal/working_balance để varchar theo core, service cast sang NUMERIC trước khi tính.';

CREATE TABLE deposit (
  deposit_id         INTEGER      PRIMARY KEY,       -- Mã sổ tiết kiệm (contract ID bên T24)
  customer_id        INTEGER      NOT NULL REFERENCES customer(customer_id),
  product_id         INTEGER      REFERENCES product(product_id),
  currency           VARCHAR(3)   NOT NULL DEFAULT 'VND',
  amount             VARCHAR(20)  NOT NULL,          -- Số tiền gốc; varchar theo core
  interest           VARCHAR(10),                    -- Lãi suất cơ sở "5.2" (%/năm)
  interest_margin    VARCHAR(10),                    -- Biên độ cộng thêm "0.3"; lãi thực = interest + interest_margin
  term               VARCHAR(5),                     -- Kỳ hạn (tháng); "0" = không kỳ hạn
  rollover           VARCHAR(20),                    -- PRINCIPAL | PRINCIPAL_INTEREST | NONE
  channel            VARCHAR(10),                    -- BRANCH | MOBILE | ONLINE
  start_date         VARCHAR(8),                     -- YYYYMMDD
  maturity_date      VARCHAR(8),                     -- YYYYMMDD; Copilot nhắc trước 3–7 ngày
  linked_account_id  INTEGER      REFERENCES account(account_id), -- TK thanh toán liên kết
  product_group      VARCHAR(20),                    -- DEPOSIT_TERM | DEPOSIT_ONLINE | DEPOSIT_FLEX
  payin_account      VARCHAR(20),                    -- TK nguồn trích tiền mở sổ
  payout_account     VARCHAR(20),                    -- TK nhận gốc/lãi khi đáo hạn/tất toán
  co_code            VARCHAR(10),
  CONSTRAINT ck_deposit_rollover CHECK (rollover IS NULL OR rollover IN ('PRINCIPAL','PRINCIPAL_INTEREST','NONE')),
  CONSTRAINT ck_deposit_channel  CHECK (channel  IS NULL OR channel  IN ('BRANCH','MOBILE','ONLINE'))
);
CREATE INDEX ix_deposit_customer ON deposit (customer_id);
COMMENT ON TABLE deposit IS 'Sổ tiết kiệm (giả lập, mô phỏng T24). Journey A: nhắc tái tục, gợi ý sản phẩm lãi cao hơn. Journey B: bản ghi đóng trước maturity_date = sự kiện SAVINGS_CLOSED (ngữ cảnh gần đây của kịch bản giả công an). Cột số/ngày để varchar theo core, service chuẩn hóa trước khi tính.';

CREATE TABLE loan (
  loan_id            INTEGER      PRIMARY KEY,       -- Mã hợp đồng vay (contract ID bên T24)
  customer_id        INTEGER      NOT NULL REFERENCES customer(customer_id),
  product_group      VARCHAR(20),                    -- LOAN_CONSUMER | LOAN_MORTGAGE | LOAN_AUTO | LOAN_BUSINESS
  product_id         INTEGER      REFERENCES product(product_id),
  account_reference  INTEGER      REFERENCES account(account_id), -- Tài khoản vay / tham chiếu hợp đồng
  currency           VARCHAR(3)   NOT NULL DEFAULT 'VND',
  amount             VARCHAR(20)  NOT NULL,          -- Số tiền vay gốc; varchar theo core
  rate               VARCHAR(10),                    -- Lãi suất cơ sở "8.5" (%/năm)
  rate_margin        VARCHAR(10),                    -- Biên độ "3.5"; lãi thực = rate + rate_margin
  term               VARCHAR(5),                     -- Kỳ hạn (tháng)
  channel            VARCHAR(10),                    -- BRANCH | MOBILE | ONLINE
  start_date         VARCHAR(8),                     -- Ngày giải ngân YYYYMMDD
  maturity_date      VARCHAR(8),                     -- Ngày đáo hạn YYYYMMDD
  payin_account      VARCHAR(20),                    -- TK trích nợ định kỳ
  payout_account     VARCHAR(20),                    -- TK nhận giải ngân
  co_code            VARCHAR(10)
);
CREATE INDEX ix_loan_customer ON loan (customer_id);
COMMENT ON TABLE loan IS 'Hợp đồng vay (giả lập, mô phỏng T24). Journey A: Copilot đọc dư nợ, kỳ hạn, ngày trích nợ để nhắc lịch trả và tính khả năng tiết kiệm. Cột số/ngày để varchar theo core, service chuẩn hóa trước khi tính.';

CREATE TABLE beneficiary (
  beneficiary_id               INTEGER      PRIMARY KEY,
  customer_id                  INTEGER      NOT NULL REFERENCES customer(customer_id),
  beneficiary_bank_code        VARCHAR(10)  NOT NULL,   -- MSB, VCB, TCB...
  beneficiary_account_no       VARCHAR(20)  NOT NULL,   -- Giả lập; KHÔNG đưa vào LLM
  beneficiary_account_masked   VARCHAR(20)  NOT NULL,   -- "0301 ****" – cột duy nhất về số TK được đưa vào LLM/UI
  beneficiary_name             VARCHAR(120) NOT NULL,   -- KHÔNG đưa vào LLM
  beneficiary_name_masked      VARCHAR(120) NOT NULL,   -- "NGUYEN VAN M***"
  beneficiary_type             VARCHAR(15)  NOT NULL DEFAULT 'PERSONAL', -- PERSONAL | MERCHANT | ORGANIZATION
  beneficiary_relationship     VARCHAR(10)  NOT NULL DEFAULT 'UNKNOWN',  -- FAMILY | FRIEND | MERCHANT | EMPLOYER | UNKNOWN (yếu tố 5)
  beneficiary_first_seen_at    TIMESTAMPTZ,             -- NULL = người nhận mới (yếu tố 2)
  beneficiary_last_tx_at       TIMESTAMPTZ,
  beneficiary_tx_count         INTEGER      NOT NULL DEFAULT 0,
  beneficiary_total_out        NUMERIC(18,0) NOT NULL DEFAULT 0, -- Tổng đã chuyển đi
  beneficiary_total_in         NUMERIC(18,0) NOT NULL DEFAULT 0, -- Tổng nhận về từ người này (mồi nhử)
  beneficiary_avg_amount       NUMERIC(18,0),
  beneficiary_age_days         INTEGER,                 -- Số ngày kể từ lần chuyển đầu
  beneficiary_is_synthetic_mule BOOLEAN     NOT NULL DEFAULT FALSE, -- TK kẻ gian trong dataset demo
  beneficiary_status           VARCHAR(10)  NOT NULL DEFAULT 'ACTIVE', -- ACTIVE | BLOCKED | SUSPECTED
  beneficiary_created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
  CONSTRAINT uq_beneficiary UNIQUE (customer_id, beneficiary_bank_code, beneficiary_account_no),
  CONSTRAINT ck_beneficiary_type   CHECK (beneficiary_type IN ('PERSONAL','MERCHANT','ORGANIZATION')),
  CONSTRAINT ck_beneficiary_rel    CHECK (beneficiary_relationship IN ('FAMILY','FRIEND','MERCHANT','EMPLOYER','UNKNOWN')),
  CONSTRAINT ck_beneficiary_status CHECK (beneficiary_status IN ('ACTIVE','BLOCKED','SUSPECTED'))
);
CREATE INDEX ix_beneficiary_customer ON beneficiary (customer_id);
COMMENT ON TABLE beneficiary IS 'Người nhận của từng khách hàng (quan hệ theo cặp KH–người nhận). first_seen_at, total_in, age_days là đầu vào trực tiếp của engine rủi ro.';

-- ---------------------------------------------------------------------
-- NHÓM 1 · BẮT BUỘC CHO DEMO
-- ---------------------------------------------------------------------

CREATE TABLE scam_scenario (
  scenario_id          VARCHAR(5)   PRIMARY KEY,     -- S01–S10, trùng key trong playbook.yaml
  scenario_name        VARCHAR(120) NOT NULL,
  group_code           VARCHAR(2)   NOT NULL,        -- G1 mạo danh | G2 deepfake | G3 đầu tư/việc làm | G4 mua bán | G5 chiếm thiết bị
  pattern              VARCHAR(10)  NOT NULL,        -- SINGLE | SERIES | DRAIN | TAKEOVER | RECEIVER
  agent_can_ask        CHAR(1)      NOT NULL DEFAULT 'Y', -- N với TAKEOVER: bỏ qua lượt hỏi, hold + sinh trắc lại
  signal_pattern       JSONB        NOT NULL,        -- Tổ hợp dấu hiệu để lookup
  questions            JSONB        NOT NULL,        -- {"SALARY":[...],"HNW":[...],"SENIOR":[...]}
  options              JSONB        NOT NULL,        -- Nút trả lời lượt 1 (2–4)
  advice_title         VARCHAR(60)  NOT NULL,
  advice_body          TEXT         NOT NULL,        -- "Mô tả ngược kịch bản"; nguồn để LLM diễn giải
  recommended_action   VARCHAR(10)  NOT NULL,        -- cancel | hold | contact | continue; LLM không được thay đổi
  priority             INTEGER      NOT NULL,        -- 0 cao nhất (TAKEOVER luôn = 0)
  status               VARCHAR(10)  NOT NULL DEFAULT 'ACTIVE', -- ACTIVE | DRAFT
  CONSTRAINT ck_scenario_pattern CHECK (pattern IN ('SINGLE','SERIES','DRAIN','TAKEOVER','RECEIVER')),
  CONSTRAINT ck_scenario_action  CHECK (recommended_action IN ('cancel','hold','contact','continue')),
  CONSTRAINT ck_scenario_ask     CHECK (agent_can_ask IN ('Y','N')),
  CONSTRAINT ck_scenario_status  CHECK (status IN ('ACTIVE','DRAFT'))
);
COMMENT ON TABLE scam_scenario IS 'Scam Playbook – bản DB của playbook.yaml. BA sở hữu nội dung, Dev 2 nạp từ YAML khi khởi động (AI-04). Hàng rào an toàn: mọi câu hỏi/khuyến cáo hiển thị đều có gốc trong bảng này, không do LLM tự nghĩ ra.';

CREATE TABLE risk_decision (
  decision_id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id          INTEGER      NOT NULL REFERENCES customer(customer_id),
  account_id           INTEGER      NOT NULL REFERENCES account(account_id),
  transaction_id       INTEGER,                      -- FK thêm ở cuối file (ref vòng); NULL tới khi POSTED
  tx_snapshot          JSONB        NOT NULL,        -- {amount, beneficiary_masked, bank_code, tx_time, memo_masked, session_flags}
  score                INTEGER      NOT NULL CHECK (score BETWEEN 0 AND 100),
  level                VARCHAR(10)  NOT NULL,        -- pass | soft_warn | intervene (< 40 / 40–74 / >= 75)
  factors              JSONB        NOT NULL,        -- 6 yếu tố: amount_deviation, new_beneficiary, time_of_day, behavior_drift, relationship_history, recent_context
  top_factors          VARCHAR(120) NOT NULL,        -- 3 key điểm cao nhất, phân cách dấu phẩy
  scenario_id          VARCHAR(5)   REFERENCES scam_scenario(scenario_id),
  template_text        TEXT         NOT NULL,        -- Lời rule-based, fallback khi LLM lỗi
  llm_reasons          JSONB,                        -- 3 câu lý do LLM
  question             TEXT,
  options              JSONB,
  selected_option      VARCHAR(120),
  free_text_masked     TEXT,                         -- Đã mask SĐT/CCCD
  advice_title         VARCHAR(60),
  advice_body          TEXT,
  recommended_action   VARCHAR(10),                  -- cancel | hold | contact | continue
  action_taken         VARCHAR(10),                  -- NULL nếu level = pass
  outcome              VARCHAR(10),                  -- prevented | held | proceeded | n/a
  created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
  intervened_at        TIMESTAMPTZ,
  actioned_at          TIMESTAMPTZ,
  CONSTRAINT ck_decision_level   CHECK (level IN ('pass','soft_warn','intervene')),
  CONSTRAINT ck_decision_reco    CHECK (recommended_action IS NULL OR recommended_action IN ('cancel','hold','contact','continue')),
  CONSTRAINT ck_decision_action  CHECK (action_taken IS NULL OR action_taken IN ('cancel','hold','contact','continue')),
  CONSTRAINT ck_decision_outcome CHECK (outcome IS NULL OR outcome IN ('prevented','held','proceeded','n/a'))
);
CREATE INDEX ix_decision_customer ON risk_decision (customer_id, created_at DESC);
CREATE INDEX ix_decision_level    ON risk_decision (level, created_at DESC);
CREATE INDEX ix_decision_tx       ON risk_decision (transaction_id);
COMMENT ON TABLE risk_decision IS 'Bảng trung tâm Journey B. Một lệnh chuyển = một dòng: /transfer/precheck tạo, /transfer/intervene và /transfer/action cập nhật cùng dòng. score/level/recommended_action do engine ghi; LLM chỉ ghi llm_reasons, question, advice_body. Mọi nội dung khách nhập phải mask trước khi lưu.';

CREATE TABLE transaction_history (
  transaction_id              INTEGER      PRIMARY KEY,   -- FT ID bên T24
  customer_id                 INTEGER      NOT NULL REFERENCES customer(customer_id),
  account_id                  INTEGER      NOT NULL REFERENCES account(account_id),
  direction                   VARCHAR(3)   NOT NULL,      -- OUT | IN; engine chỉ chấm OUT
  beneficiary_id              INTEGER      REFERENCES beneficiary(beneficiary_id), -- NULL nếu chuyển nội bộ cùng chủ
  beneficiary_bank_code       VARCHAR(10),                -- Lưu tại thời điểm giao dịch
  beneficiary_account_masked  VARCHAR(20),                -- "0301 ****"
  currency                    VARCHAR(3)   NOT NULL DEFAULT 'VND',
  amount                      VARCHAR(20)  NOT NULL,      -- varchar theo core
  balance_after               VARCHAR(20),                -- Số dư sau giao dịch; drain_ratio lịch sử
  transaction_type            VARCHAR(10),                -- FT | BILL | QR | ATM | SALARY
  category                    VARCHAR(20),                -- FOOD | TRANSPORT | SHOPPING | BILLS | RENT | HEALTH | FAMILY_SUPPORT | INVESTMENT | TRANSFER_P2P | OTHER
  transaction_description     VARCHAR(255),               -- Nội dung CK; engine quét từ khóa; mask nếu chứa SĐT/CCCD
  channel                     VARCHAR(10),                -- MOBILE | INTERNET | BRANCH | ATM
  transaction_date            VARCHAR(8)   NOT NULL,      -- YYYYMMDD
  transaction_time            VARCHAR(6)   NOT NULL,      -- HHMMSS
  status                      VARCHAR(10)  NOT NULL DEFAULT 'POSTED', -- POSTED | PENDING (Guardian khóa tạm) | CANCELLED | REVERSED
  risk_decision_id            UUID         REFERENCES risk_decision(decision_id), -- NULL với lịch sử seed
  is_fraud                    CHAR(1)      NOT NULL DEFAULT 'N', -- Y | N; chỉ dùng kiểm thử, KHÔNG đưa vào engine/LLM
  fraud_case_id               VARCHAR(5),                 -- F01–F10; FK thêm ở cuối file
  co_code                     VARCHAR(10),
  CONSTRAINT ck_tx_direction CHECK (direction IN ('OUT','IN')),
  CONSTRAINT ck_tx_status    CHECK (status IN ('POSTED','PENDING','CANCELLED','REVERSED')),
  CONSTRAINT ck_tx_is_fraud  CHECK (is_fraud IN ('Y','N'))
);
CREATE INDEX ix_tx_customer_time ON transaction_history (customer_id, transaction_date, transaction_time);
CREATE INDEX ix_tx_benef_date    ON transaction_history (beneficiary_id, transaction_date);
CREATE INDEX ix_tx_status        ON transaction_history (status);
COMMENT ON TABLE transaction_history IS 'Lịch sử giao dịch 3 tháng (giả lập, mô phỏng T24 FUNDS.TRANSFER). Nguồn duy nhất tính behavior_profile; nơi generator inject 10 fraud case. Baseline chỉ tính direction = OUT, status = POSTED, is_fraud = N. is_fraud/fraud_case_id phải bị loại khỏi mọi API trả UI và mọi prompt LLM.';

CREATE TABLE behavior_profile (
  profile_id                 SERIAL       PRIMARY KEY,
  customer_id                INTEGER      NOT NULL UNIQUE REFERENCES customer(customer_id),
  window_days                INTEGER      NOT NULL DEFAULT 90,
  out_median                 NUMERIC(18,0) NOT NULL,
  out_p90                    NUMERIC(18,0) NOT NULL,     -- vượt = UNUSUAL
  out_p99                    NUMERIC(18,0) NOT NULL,     -- vượt = HIGH
  out_max                    NUMERIC(18,0) NOT NULL,
  monthly_out_avg            NUMERIC(18,0) NOT NULL,
  monthly_in_avg             NUMERIC(18,0) NOT NULL,
  known_beneficiaries        INTEGER      NOT NULL,
  new_benef_per_30d          NUMERIC(6,2) NOT NULL,      -- HNW 4–5, SENIOR 0.2
  share_to_new_benef         NUMERIC(5,4) NOT NULL,      -- 0–1
  active_hours               JSONB        NOT NULL,      -- 24 phần tử, tỷ lệ theo giờ
  night_tx_ratio             NUMERIC(5,4) NOT NULL,      -- 23:00–05:59
  weekend_tx_ratio           NUMERIC(5,4) NOT NULL,
  tx_per_week                NUMERIC(6,2) NOT NULL,
  max_tx_per_day             INTEGER      NOT NULL,      -- bắt việc nhẹ lương cao
  max_cum_to_one_benef_14d   NUMERIC(18,0) NOT NULL,     -- bắt mẫu chuỗi đầu tư/romance
  balance_median             NUMERIC(18,0) NOT NULL,
  max_drain_ratio_90d        NUMERIC(5,4) NOT NULL,      -- bắt "vét sạch"
  computed_at                TIMESTAMPTZ  NOT NULL DEFAULT now(),
  feedback_version           INTEGER      NOT NULL DEFAULT 0
);
COMMENT ON TABLE behavior_profile IS 'Digital Twin – baseline hành vi từng khách hàng, tính từ transaction_history (OUT, POSTED, is_fraud = N). Precheck so với bảng này, không quét lại lịch sử, để giữ < 300 ms. Không có PII.';

CREATE TABLE llm_trace (
  trace_id       BIGSERIAL    PRIMARY KEY,
  decision_id    UUID         REFERENCES risk_decision(decision_id), -- NULL với Copilot
  customer_id    INTEGER      REFERENCES customer(customer_id),
  agent          VARCHAR(20)  NOT NULL,   -- copilot | shield_explain | shield_interview | shield_advice
  model          VARCHAR(50)  NOT NULL,
  prompt_key     VARCHAR(50)  NOT NULL,   -- "shield_explain@v3"
  prompt_masked  TEXT         NOT NULL,   -- Đã mask PII
  response       TEXT,
  latency_ms     INTEGER,
  status         VARCHAR(10)  NOT NULL,   -- ok | timeout | error | cache
  created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
  CONSTRAINT ck_trace_status CHECK (status IN ('ok','timeout','error','cache'))
);
CREATE INDEX ix_trace_decision ON llm_trace (decision_id);
CREATE INDEX ix_trace_agent    ON llm_trace (agent, created_at DESC);
COMMENT ON TABLE llm_trace IS 'Log mọi lượt gọi LLM của cả hai journey – bằng chứng "AI kiểm toán được". Không ghi PII chưa mask; bản ghi timeout/error vẫn phải tồn tại để tính tỷ lệ fallback.';

CREATE TABLE guardian_case (                             -- DBML: "case" (từ khóa SQL)
  case_id                VARCHAR(20)  PRIMARY KEY,       -- CASE-2026-0001
  decision_id            UUID         NOT NULL UNIQUE REFERENCES risk_decision(decision_id), -- 1-1
  customer_id            INTEGER      NOT NULL REFERENCES customer(customer_id),
  scenario_id            VARCHAR(5)   REFERENCES scam_scenario(scenario_id),
  status                 VARCHAR(15)  NOT NULL DEFAULT 'OPEN', -- OPEN | CALLBACK_DONE | CLOSED_FRAUD | CLOSED_LEGIT
  narrative              TEXT         NOT NULL,          -- Tóm tắt tự sinh, đã mask
  callback_phone_masked  VARCHAR(15),                    -- "09** *** 123"
  created_at             TIMESTAMPTZ  NOT NULL DEFAULT now(),
  closed_at              TIMESTAMPTZ,
  CONSTRAINT ck_case_status CHECK (status IN ('OPEN','CALLBACK_DONE','CLOSED_FRAUD','CLOSED_LEGIT'))
);
CREATE INDEX ix_case_status ON guardian_case (status, created_at DESC);
COMMENT ON TABLE guardian_case IS 'Case mock thay hệ thống case của Khối Rủi ro. Mở tự động khi action_taken = hold hoặc contact. CLOSED_FRAUD/CLOSED_LEGIT là nguồn sinh feedback (source = ops). Sau hackathon thay bằng adapter tới hệ thống thật.';

CREATE TABLE fraud_case (
  fraud_case_id        VARCHAR(5)   PRIMARY KEY,       -- F01–F10
  customer_id          INTEGER      NOT NULL REFERENCES customer(customer_id),
  scenario_id          VARCHAR(5)   NOT NULL REFERENCES scam_scenario(scenario_id),
  pattern              VARCHAR(10)  NOT NULL,          -- copy từ scam_scenario
  description          VARCHAR(255) NOT NULL,
  injection            JSONB        NOT NULL,          -- pre_events, tx/series, inbound, session flags
  expected_level       VARCHAR(10)  NOT NULL,          -- cho GIAO DỊCH CUỐI của case
  expected_score_min   INTEGER      NOT NULL,
  expected_score_max   INTEGER      NOT NULL,
  demo_scene           VARCHAR(20),                    -- "Scene 3-5"; NULL nếu chỉ kiểm thử
  last_test_score      INTEGER,
  last_test_passed     CHAR(1),                        -- Y | N
  last_test_at         TIMESTAMPTZ,
  CONSTRAINT ck_fraud_level  CHECK (expected_level IN ('pass','soft_warn','intervene')),
  CONSTRAINT ck_fraud_passed CHECK (last_test_passed IS NULL OR last_test_passed IN ('Y','N'))
);
COMMENT ON TABLE fraud_case IS 'Spec 10 fraud case: vừa hướng dẫn sinh dữ liệu (BA-02, BE-03) vừa là bộ kiểm thử engine (BE-05). Cặp F01/F02 là bằng chứng false positive khi pitch. KHÔNG expose ra API khách hàng.';

-- ---------------------------------------------------------------------
-- NHÓM 2 · CLOSED-LOOP
-- ---------------------------------------------------------------------

CREATE TABLE account_event (
  event_id     BIGSERIAL    PRIMARY KEY,
  customer_id  INTEGER      NOT NULL REFERENCES customer(customer_id),
  account_id   INTEGER      REFERENCES account(account_id),  -- NULL với sự kiện cấp khách hàng
  event_type   VARCHAR(20)  NOT NULL,   -- SAVINGS_CLOSED | LIMIT_RAISED | NEW_DEVICE_LOGIN | PASSWORD_RESET | INBOUND_UNKNOWN
  amount       NUMERIC(18,0),
  event_time   TIMESTAMPTZ  NOT NULL,   -- Yếu tố "ngữ cảnh gần đây" chỉ xét 60 phút trước giao dịch
  source_ref   VARCHAR(50),             -- deposit_id, transaction_id hoặc device_id
  meta         JSONB        NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT ck_event_type CHECK (event_type IN ('SAVINGS_CLOSED','LIMIT_RAISED','NEW_DEVICE_LOGIN','PASSWORD_RESET','INBOUND_UNKNOWN'))
);
CREATE INDEX ix_event_customer_time ON account_event (customer_id, event_time DESC);
COMMENT ON TABLE account_event IS 'Sự kiện ngoài chuyển tiền – đầu vào yếu tố rủi ro số 6. Không có bảng này thì S01 (tất toán rồi chuyển) và S10 (nhận lạ rồi chuyển trả) không bắt được. Generator sinh từ deposit và transaction_history; tách bảng để precheck đọc một truy vấn.';

CREATE TABLE feedback (
  feedback_id          BIGSERIAL    PRIMARY KEY,
  decision_id          UUID         NOT NULL REFERENCES risk_decision(decision_id),
  customer_id          INTEGER      NOT NULL REFERENCES customer(customer_id),
  label                VARCHAR(10)  NOT NULL,   -- fraud | legit
  source               VARCHAR(10)  NOT NULL,   -- customer | ops
  note                 VARCHAR(255),            -- Không PII
  applied_to_baseline  CHAR(1)      NOT NULL DEFAULT 'N', -- Y | N
  created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
  applied_at           TIMESTAMPTZ,
  CONSTRAINT ck_feedback_label   CHECK (label IN ('fraud','legit')),
  CONSTRAINT ck_feedback_source  CHECK (source IN ('customer','ops')),
  CONSTRAINT ck_feedback_applied CHECK (applied_to_baseline IN ('Y','N'))
);
CREATE INDEX ix_feedback_pending ON feedback (applied_to_baseline, created_at);
COMMENT ON TABLE feedback IS 'Nhãn phản hồi đóng vòng closed-loop. legit → đưa vào baseline, beneficiary tin cậy; fraud → loại khỏi baseline, beneficiary SUSPECTED. Mỗi lần áp dụng tăng behavior_profile.feedback_version.';

CREATE TABLE notification (
  notification_id  BIGSERIAL    PRIMARY KEY,
  customer_id      INTEGER      NOT NULL REFERENCES customer(customer_id),
  decision_id      UUID         REFERENCES risk_decision(decision_id), -- NULL với Copilot
  channel          VARCHAR(15)  NOT NULL,   -- push | sms | call_request
  template_key     VARCHAR(50)  NOT NULL,   -- post_continue_warning, hold_confirmed, callback_scheduled, copilot_monthly_insight
  payload          JSONB        NOT NULL,   -- Đã render, đã mask
  status           VARCHAR(10)  NOT NULL DEFAULT 'SENT', -- SENT | DELIVERED | ACTIONED | EXPIRED
  sent_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
  actioned_at      TIMESTAMPTZ,
  CONSTRAINT ck_notif_channel CHECK (channel IN ('push','sms','call_request')),
  CONSTRAINT ck_notif_status  CHECK (status IN ('SENT','DELIVERED','ACTIONED','EXPIRED'))
);
CREATE INDEX ix_notif_customer ON notification (customer_id, sent_at DESC);
COMMENT ON TABLE notification IS 'Kênh thông báo mock. Quan trọng nhất: post_continue_warning gửi sau 5 phút khi khách chọn "Vẫn tiếp tục" ở mức intervene, cho phép khóa giao dịch trong cửa sổ hold.';

-- ---------------------------------------------------------------------
-- NHÓM 3 · JOURNEY A – FINANCIAL COPILOT
-- ---------------------------------------------------------------------

CREATE TABLE spending_insight (
  insight_id       BIGSERIAL    PRIMARY KEY,
  customer_id      INTEGER      NOT NULL REFERENCES customer(customer_id),
  period           VARCHAR(6)   NOT NULL,   -- YYYYMM
  category         VARCHAR(20)  NOT NULL,   -- theo transaction_history.category; TOTAL cho dòng tổng
  amount           NUMERIC(18,0) NOT NULL,
  delta_vs_prev    NUMERIC(8,2),            -- % so kỳ trước
  rank_in_period   INTEGER,                 -- 1 = lớn nhất
  insight_text     TEXT,                    -- LLM sinh từ số đã tính
  generated_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
  CONSTRAINT uq_insight UNIQUE (customer_id, period, category)
);
COMMENT ON TABLE spending_insight IS 'Phân tích dòng tiền của Copilot, tính sẵn và cache để Scene 1 không gọi LLM lại. Số liệu do service tính; LLM chỉ sinh insight_text.';

CREATE TABLE product_recommendation (
  recommendation_id  BIGSERIAL    PRIMARY KEY,
  customer_id        INTEGER      NOT NULL REFERENCES customer(customer_id),
  product_id         INTEGER      NOT NULL REFERENCES product(product_id), -- chỉ ACTIVE
  insight_id         BIGINT       REFERENCES spending_insight(insight_id),
  reason             VARCHAR(255),            -- LLM diễn giải theo persona, < 2 câu
  estimated_benefit  NUMERIC(18,0),           -- Service tính, không do LLM
  status             VARCHAR(10)  NOT NULL DEFAULT 'SHOWN', -- SHOWN | ACCEPTED | DISMISSED
  shown_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
  responded_at       TIMESTAMPTZ,
  CONSTRAINT ck_reco_status CHECK (status IN ('SHOWN','ACCEPTED','DISMISSED'))
);
CREATE INDEX ix_reco_customer ON product_recommendation (customer_id, shown_at DESC);
COMMENT ON TABLE product_recommendation IS 'Gợi ý sản phẩm của Copilot gắn với bảng product; số liệu cross-sell (ACCEPTED/SHOWN). Mỗi gợi ý phải trỏ về một insight_id. Không gợi ý LOAN trong phạm vi demo.';

-- ---------------------------------------------------------------------
-- KHÓA NGOẠI VÒNG & KHÓA NGOẠI TẠO SAU
-- ---------------------------------------------------------------------
ALTER TABLE risk_decision
  ADD CONSTRAINT fk_decision_transaction
  FOREIGN KEY (transaction_id) REFERENCES transaction_history(transaction_id);

ALTER TABLE transaction_history
  ADD CONSTRAINT fk_tx_fraud_case
  FOREIGN KEY (fraud_case_id) REFERENCES fraud_case(fraud_case_id);

-- ---------------------------------------------------------------------
-- VIEW cho Ops view
-- ---------------------------------------------------------------------
CREATE VIEW ops_summary AS
SELECT
  count(*) FILTER (WHERE level <> 'pass')                               AS suspicious_total,
  count(*) FILTER (WHERE outcome IN ('prevented','held'))               AS prevented_total,
  count(*) FILTER (WHERE level = 'intervene' AND outcome = 'proceeded') AS proceeded_after_intervene,
  round(avg(EXTRACT(EPOCH FROM (actioned_at - created_at))))            AS avg_response_sec
FROM risk_decision;

CREATE VIEW ops_decision_log AS
SELECT d.decision_id, d.created_at, c.target AS persona_code,
       (d.tx_snapshot->>'amount')::numeric AS amount,
       d.score, d.level, d.top_factors, d.scenario_id, s.scenario_name,
       d.selected_option, d.action_taken, d.outcome
FROM risk_decision d
JOIN customer c ON c.customer_id = d.customer_id
LEFT JOIN scam_scenario s ON s.scenario_id = d.scenario_id
ORDER BY d.created_at DESC;

COMMIT;

-- ===========================================================================
-- Danh tính và phân quyền của người dùng ứng dụng (identity-service sở hữu)
--
-- Hai bảng này được tạo trực tiếp trên database sau khi 18 bảng nghiệp vụ đã
-- chạy; phần khai báo dưới đây chép lại đúng cấu trúc đang có để file schema
-- vẫn là mô tả đầy đủ của database.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS app_role (
    role_code    VARCHAR      PRIMARY KEY,
    role_name    VARCHAR      NOT NULL,
    role_scope   VARCHAR      NOT NULL,
    -- Danh sách quyền dạng chuỗi, ví dụ ["ops.read","ops.decide"].
    permissions  JSONB        NOT NULL DEFAULT '[]'::jsonb,
    role_status  VARCHAR      NOT NULL DEFAULT 'ACTIVE',
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app_user (
    user_id            BIGSERIAL    PRIMARY KEY,
    username           VARCHAR      NOT NULL,
    -- bcrypt, 60 ký tự. KHÔNG endpoint nào được trả cột này ra ngoài.
    password_hash      VARCHAR      NOT NULL,
    role               VARCHAR      NOT NULL,
    -- NULL với tài khoản nội bộ (ADMIN); có giá trị với tài khoản khách hàng.
    customer_id        INTEGER      REFERENCES customer(customer_id),
    full_name          VARCHAR,
    email              VARCHAR,
    phone_no           VARCHAR,
    -- ACTIVE | DISABLED (quản trị viên tắt) | LOCKED (tự khoá do sai mật khẩu)
    user_status        VARCHAR      NOT NULL DEFAULT 'ACTIVE',
    failed_login_count INTEGER      NOT NULL DEFAULT 0,
    last_login_at      TIMESTAMPTZ,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Database hiện chưa có ràng buộc duy nhất trên username. Tra cứu theo tên đăng
-- nhập vì vậy lấy bản ghi đầu tiên; thêm ràng buộc này khi dữ liệu đã sạch:
--   CREATE UNIQUE INDEX app_user_username_key ON app_user (username);
