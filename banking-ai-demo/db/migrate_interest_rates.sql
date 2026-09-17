-- =====================================================================
-- Migration: thêm biểu lãi suất tiết kiệm (interest_rate_term + interest_rate)
-- cho database ĐANG CHẠY mà không cần make db-reset (giữ nguyên dữ liệu demo).
--
-- Chạy:  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrate_interest_rates.sql
--
-- Idempotent: chạy lại bao nhiêu lần cũng ra cùng trạng thái. Database dựng mới
-- thì KHÔNG cần file này — schema + seed đã bao gồm hai bảng.
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS interest_rate_term (
  term_code    VARCHAR(10)  PRIMARY KEY,
  term_months  INTEGER      NOT NULL UNIQUE,
  term_label   VARCHAR(30)  NOT NULL
);
COMMENT ON TABLE interest_rate_term IS 'Danh mục kỳ hạn gửi tiết kiệm. Dữ liệu tham chiếu, seed một lần; màn Biểu lãi suất dùng term_months để xếp thứ tự cột.';

CREATE TABLE IF NOT EXISTS interest_rate (
  rate_id        INTEGER      PRIMARY KEY,
  product_id     INTEGER      NOT NULL REFERENCES product(product_id),
  term_code      VARCHAR(10)  NOT NULL REFERENCES interest_rate_term(term_code),
  rate_pct       NUMERIC(5,2) NOT NULL,
  effective_from DATE         NOT NULL,
  CONSTRAINT uq_interest_rate UNIQUE (product_id, term_code, effective_from)
);
COMMENT ON TABLE interest_rate IS 'Lãi suất %/năm theo (sản phẩm, kỳ hạn, đợt hiệu lực). Màn Biểu lãi suất lấy đợt mới nhất chưa vượt ngày hiện tại; giữ các đợt cũ để đối chiếu lịch sử điều chỉnh.';

INSERT INTO interest_rate_term (term_code, term_months, term_label) VALUES
  ('KKH', 0, 'Không kỳ hạn'),
  ('T01', 1, '1 tháng'),
  ('T03', 3, '3 tháng'),
  ('T06', 6, '6 tháng'),
  ('T09', 9, '9 tháng'),
  ('T12', 12, '12 tháng'),
  ('T18', 18, '18 tháng'),
  ('T24', 24, '24 tháng')
ON CONFLICT (term_code) DO UPDATE
  SET term_months = EXCLUDED.term_months, term_label = EXCLUDED.term_label;

INSERT INTO interest_rate (rate_id, product_id, term_code, rate_pct, effective_from) VALUES
  (1, 1, 'T01', 3.4, '2026-06-01'),
  (2, 1, 'T03', 3.7, '2026-06-01'),
  (3, 1, 'T06', 5.0, '2026-06-01'),
  (4, 1, 'T09', 5.1, '2026-06-01'),
  (5, 1, 'T12', 5.3, '2026-06-01'),
  (6, 1, 'T18', 5.4, '2026-06-01'),
  (7, 1, 'T24', 5.4, '2026-06-01'),
  (8, 2, 'T01', 3.7, '2026-06-01'),
  (9, 2, 'T03', 4.0, '2026-06-01'),
  (10, 2, 'T06', 5.3, '2026-06-01'),
  (11, 2, 'T09', 5.5, '2026-06-01'),
  (12, 2, 'T12', 5.6, '2026-06-01'),
  (13, 2, 'T18', 5.8, '2026-06-01'),
  (14, 2, 'T24', 5.9, '2026-06-01'),
  (15, 3, 'KKH', 0.3, '2026-06-01'),
  (16, 4, 'T03', 3.8, '2026-06-01'),
  (17, 4, 'T06', 5.1, '2026-06-01'),
  (18, 4, 'T09', 5.4, '2026-06-01'),
  (19, 4, 'T12', 5.9, '2026-06-01'),
  (20, 4, 'T18', 6.0, '2026-06-01'),
  (21, 4, 'T24', 6.1, '2026-06-01'),
  (22, 7, 'KKH', 0.1, '2026-06-01'),
  (23, 1, 'T01', 3.6, '2026-09-01'),
  (24, 1, 'T03', 3.9, '2026-09-01'),
  (25, 1, 'T06', 5.2, '2026-09-01'),
  (26, 1, 'T09', 5.3, '2026-09-01'),
  (27, 1, 'T12', 5.5, '2026-09-01'),
  (28, 1, 'T18', 5.6, '2026-09-01'),
  (29, 1, 'T24', 5.6, '2026-09-01'),
  (30, 2, 'T01', 3.9, '2026-09-01'),
  (31, 2, 'T03', 4.2, '2026-09-01'),
  (32, 2, 'T06', 5.5, '2026-09-01'),
  (33, 2, 'T09', 5.7, '2026-09-01'),
  (34, 2, 'T12', 5.8, '2026-09-01'),
  (35, 2, 'T18', 6.0, '2026-09-01'),
  (36, 2, 'T24', 6.1, '2026-09-01'),
  (37, 3, 'KKH', 0.5, '2026-09-01'),
  (38, 4, 'T03', 4.0, '2026-09-01'),
  (39, 4, 'T06', 5.3, '2026-09-01'),
  (40, 4, 'T09', 5.6, '2026-09-01'),
  (41, 4, 'T12', 6.1, '2026-09-01'),
  (42, 4, 'T18', 6.2, '2026-09-01'),
  (43, 4, 'T24', 6.3, '2026-09-01'),
  (44, 7, 'KKH', 0.1, '2026-09-01')
ON CONFLICT (product_id, term_code, effective_from) DO UPDATE
  SET rate_pct = EXCLUDED.rate_pct;

COMMIT;
