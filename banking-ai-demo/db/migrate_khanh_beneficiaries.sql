-- =====================================================================
-- Migration: thêm 2 người nhận cùng tên "KHANH" cho khách demo 100008
-- vào database ĐANG CHẠY, phục vụ kịch bản Chat Banking hỏi lại khi tên
-- người nhận mơ hồ ("chuyển 500k cho anh Khánh" → chọn Khánh nào).
--
-- Chạy:  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrate_khanh_beneficiaries.sql
--
-- Idempotent: ON CONFLICT DO NOTHING — chạy lại không nhân bản dòng.
-- Database dựng mới KHÔNG cần file này: seed.sql đã bao gồm (mục 6b).
-- =====================================================================

BEGIN;

INSERT INTO beneficiary (beneficiary_id, customer_id, beneficiary_bank_code, beneficiary_account_no, beneficiary_account_masked, beneficiary_name, beneficiary_name_masked, beneficiary_type, beneficiary_relationship, beneficiary_first_seen_at, beneficiary_last_tx_at, beneficiary_tx_count, beneficiary_total_out, beneficiary_total_in, beneficiary_avg_amount, beneficiary_age_days, beneficiary_is_synthetic_mule, beneficiary_status) VALUES
  (300901, 100008, 'MSB', '0330168839210', '0330 ****', 'PHAM QUOC KHANH', 'PHAM QUOC K***', 'PERSONAL', 'FRIEND', '2025-10-05 10:00:00+07', '2026-09-12 14:00:00+07', 14, 21400000, 0, 1528571, 345, FALSE, 'ACTIVE'),
  (300902, 100008, 'VCB', '9704229981', '9704 ****', 'TRAN DUY KHANH', 'TRAN DUY K***', 'PERSONAL', 'FAMILY', '2026-01-18 10:00:00+07', '2026-09-08 14:00:00+07', 6, 7800000, 0, 1300000, 240, FALSE, 'ACTIVE')
ON CONFLICT (beneficiary_id) DO NOTHING;

COMMIT;
