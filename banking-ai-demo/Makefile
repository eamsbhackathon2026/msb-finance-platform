# Makefile cho banking-ai-demo
# Yêu cầu: python3.12+, docker, kubectl, psql (cho các target db-*)

SERVICES := customer-profile-service transaction-service risk-scoring-service scam-knowledge-service action-feedback-service identity-service

REGISTRY ?= vcr.vngcloud.vn/114544-ea-hackathon
TAG ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo latest)

SERVICE ?= customer-profile-service
PORT ?= 8080
PYTHON ?= python3.12

REGISTRY_HOST ?= vcr.vngcloud.vn
NAMESPACE ?= finance-demo
PULL_SECRET ?= vcr-cred


# psql không phải lúc nào cũng có sẵn, nhưng máy build được image thì luôn có
# docker. PSQL tự chọn psql cục bộ nếu có, không thì chạy qua container.
# --network=host để container nối được tới database bên ngoài.
PSQL_IMAGE ?= postgres:16-alpine
PSQL := $(shell command -v psql 2>/dev/null)
ifeq ($(PSQL),)
  PSQL_RUN = docker run --rm -i --network=host -e PGPASSWORD \
             $(PSQL_IMAGE) psql
else
  PSQL_RUN = psql
endif

SCHEMA := ../msb_guardian_schema.sql
SEED   := db/seed.sql

.PHONY: help test build run sync-common k8s-apply k8s-secret k8s-db-secret \
        k8s-db-check k8s-verify deploy-prep redeploy port-forward \
        db-init db-seed db-reset db-gen-seed db-test db-ping db-status dev-up dev-down

help:
	@echo "Phát triển"
	@echo "  make sync-common  - đồng bộ services/_common/common.py vào 5 service"
	@echo "  make test         - chạy pytest cho tất cả service (không cần database)"
	@echo "  make run          - chạy 1 service local (SERVICE=<tên> PORT=<cổng>)"
	@echo "  make dev-up       - dựng Postgres trong docker + nạp schema + seed"
	@echo "  make dev-down     - xóa Postgres dev"
	@echo ""
	@echo "Database  (cần DATABASE_URL)"
	@echo "  make db-ping      - thử kết nối (chạy cái này TRƯỚC TIÊN)"
	@echo "  make db-status    - xem database đã có schema và dữ liệu chưa"
	@echo "  make db-init      - nạp schema 18 bảng"
	@echo "  make db-gen-seed  - sinh lại db/seed.sql từ generator"
	@echo "  make db-seed      - nạp dữ liệu mẫu 10 khách hàng"
	@echo "  make db-reset     - db-init + db-seed"
	@echo "  make db-test      - chấm engine trên 10 fraud case (cần service đang chạy)"
	@echo ""
	@echo "Đóng gói & triển khai"
	@echo "  make build        - docker build tất cả service, tag = $(TAG)"
	@echo "  make k8s-db-secret- tạo secret chứa DATABASE_URL"
	@echo "  make k8s-secret   - tạo imagePullSecret '$(PULL_SECRET)'"
	@echo "  make k8s-apply    - apply toàn bộ manifest"
	@echo ""
	@echo "Đưa lên VKS lần đầu  (cần DATABASE_URL + KUBECONFIG)"
	@echo "  make deploy-prep  - ping DB, nạp schema+seed, tạo secret, kiểm tra từ trong cluster"
	@echo "  make redeploy     - apply manifest + rollout lại 5 service"
	@echo "  make k8s-verify   - xem image đang chạy và kết nối DB của từng service"
	@echo "  make port-forward - mở cả 5 service ra localhost:8081-8085"

# --- Phát triển -------------------------------------------------------------
# Docker build context là services/<tên> nên mỗi service phải có bản copy riêng
# của common.py. services/_common/common.py là bản gốc duy nhất được sửa.
sync-common:
	@for svc in $(SERVICES); do \
		cp services/_common/common.py services/$$svc/common.py; \
		echo "  → services/$$svc/common.py"; \
	done
	@echo "Đã đồng bộ common.py vào $(words $(SERVICES)) service."

test: sync-common
	@failed=0; \
	for svc in $(SERVICES); do \
		echo "==> test $$svc"; \
		( cd services/$$svc && $(PYTHON) -m pytest -q ) || failed=1; \
	done; \
	exit $$failed

run: sync-common
	@echo "==> $(SERVICE) tại http://localhost:$(PORT)/docs  (Ctrl+C để dừng)"
	cd services/$(SERVICE) && $(PYTHON) -m pip install -q -r requirements.txt && \
		$(PYTHON) -m uvicorn main:app --reload --host 0.0.0.0 --port $(PORT)

# --- Database ---------------------------------------------------------------
db-init:
	@test -n "$(DATABASE_URL)" || { echo "ERROR: chưa đặt DATABASE_URL"; exit 1; }
	$(PSQL_RUN) "$(DATABASE_URL)" -v ON_ERROR_STOP=1 < $(SCHEMA)
	@echo "Đã nạp schema."

db-gen-seed:
	$(PYTHON) db/generate_seed.py > $(SEED)
	@echo "Đã sinh $(SEED)."

db-seed:
	@test -n "$(DATABASE_URL)" || { echo "ERROR: chưa đặt DATABASE_URL"; exit 1; }
	$(PSQL_RUN) "$(DATABASE_URL)" -v ON_ERROR_STOP=1 < $(SEED)
	@echo "Đã nạp dữ liệu mẫu."

db-ping:
	@test -n "$(DATABASE_URL)" || { echo "ERROR: chưa đặt DATABASE_URL"; exit 1; }
	@echo "Đang thử kết nối..."
	@$(PSQL_RUN) "$(DATABASE_URL)" -tAc \
		"SELECT 'Kết nối OK · ' || current_database() || ' · ' || version()" \
		|| { echo ""; echo "Không kết nối được. Kiểm tra host, cổng, mật khẩu và sslmode."; exit 1; }

db-status:
	@test -n "$(DATABASE_URL)" || { echo "ERROR: chưa đặt DATABASE_URL"; exit 1; }
	@# Tách làm hai bước: PostgreSQL phân tích cả nhánh ELSE của CASE nên gộp vào
	@# một câu lệnh sẽ gãy ngay lúc parse khi bảng chưa tồn tại.
	@if [ "$$($(PSQL_RUN) "$(DATABASE_URL)" -tAc \
	        "SELECT to_regclass('public.customer') IS NOT NULL" 2>/dev/null)" = "t" ]; then \
		$(PSQL_RUN) "$(DATABASE_URL)" -tAc "SELECT 'Schema OK · khách hàng: ' \
		  || (SELECT count(*) FROM customer) || ' · giao dịch: ' \
		  || (SELECT count(*) FROM transaction_history) || ' · fraud case: ' \
		  || (SELECT count(*) FROM fraud_case)"; \
	else \
		echo "Chưa nạp schema — chạy: make db-reset"; \
	fi

db-reset: db-init db-seed

db-test:
	@test -n "$(DATABASE_URL)" || { echo "ERROR: chưa đặt DATABASE_URL"; exit 1; }
	$(PYTHON) db/run_fraud_tests.py

# Postgres dùng cho phát triển local, dữ liệu nằm trong container nên xóa là sạch.
dev-up:
	docker rm -f msb-dev-db 2>/dev/null || true
	docker run -d --name msb-dev-db -e POSTGRES_PASSWORD=guardian \
		-e POSTGRES_DB=guardian -p 5432:5432 postgres:16-alpine
	@echo "Chờ Postgres sẵn sàng..."
	@for i in $$(seq 1 40); do \
		docker exec msb-dev-db pg_isready -U postgres -d guardian >/dev/null 2>&1 && break; \
		sleep 1; \
	done
	docker exec -i msb-dev-db psql -U postgres -d guardian -v ON_ERROR_STOP=1 < $(SCHEMA)
	docker exec -i msb-dev-db psql -U postgres -d guardian -v ON_ERROR_STOP=1 < $(SEED)
	@echo ""
	@echo "Sẵn sàng. Dùng:"
	@echo "  export DATABASE_URL='postgresql://postgres:guardian@localhost:5432/guardian'"

dev-down:
	docker rm -f msb-dev-db 2>/dev/null || true


# Kiểm tra database từ BÊN TRONG cluster. Laptop và cluster đi ra Internet bằng
# hai địa chỉ khác nhau, mà PostgreSQL trên VNG Cloud thường lọc theo IP hoặc VPC —
# nên "máy tôi kết nối được" không chứng minh được pod cũng kết nối được. Đây là
# nguyên nhân hay gặp khi pod lên xanh nhưng mọi endpoint nghiệp vụ đều lỗi.
k8s-db-check:
	@test -n "$(DATABASE_URL)" || { echo "ERROR: chưa đặt DATABASE_URL"; exit 1; }
	@echo "Đang thử kết nối database từ trong namespace $(NAMESPACE)..."
	@kubectl -n $(NAMESPACE) run guardian-db-check-$$$$ \
		--rm -i --restart=Never --image=$(PSQL_IMAGE) --command -- \
		psql "$(DATABASE_URL)" -tAc \
		"SELECT 'Pod kết nối được · ' || current_database()" \
	|| { echo ""; \
	     echo "Pod KHÔNG kết nối được tới database."; \
	     echo "Thường do PostgreSQL chặn IP ra của cluster. Mở whitelist cho địa chỉ"; \
	     echo "ra Internet của VKS, hoặc đặt database và cluster trong cùng VPC."; \
	     exit 1; }

# Toàn bộ phần chuẩn bị cho một lần deploy thành công, theo đúng thứ tự phụ thuộc.
deploy-prep: db-ping db-reset k8s-db-secret k8s-db-check
	@echo ""
	@echo "Đã sẵn sàng deploy. Kích hoạt lại CD bằng:  make redeploy"

# Deploy đúng image của commit hiện tại, giống hệt cách CI làm.
#
# Không thể chỉ apply manifest rồi rollout restart: manifest để image :latest với
# imagePullPolicy IfNotPresent, nên node đã cache :latest từ lần deploy trước sẽ
# dùng lại bản cũ — pod khởi động lại nhưng vẫn chạy code cũ, và nhìn bên ngoài
# thì mọi thứ đều "xanh". Gắn thẳng commit SHA làm tag để không có chỗ cho nhầm lẫn.
# Không dùng HEAD: workflow chỉ chạy khi thư mục service hoặc _common thay đổi, nên
# một commit chỉ sửa Makefile hay README sẽ KHÔNG có image nào được build. Lấy commit
# gần nhất thực sự chạm vào code service — đó đúng là tag mà CI đã gắn.
DEPLOY_TAG ?= $(shell git log -1 --format=%H -- services/ ../.github/workflows/)

redeploy:
	@echo "Deploy commit $(DEPLOY_TAG)"
	kubectl -n $(NAMESPACE) apply -f k8s/config.yaml
	@for svc in $(SERVICES); do \
		sed "s#:latest#:$(DEPLOY_TAG)#" k8s/$$(echo $$svc | sed 's/-service$$//').yaml \
			| kubectl -n $(NAMESPACE) apply -f - || exit 1; \
	done
	@for svc in $(SERVICES); do \
		kubectl -n $(NAMESPACE) rollout status deployment/$$svc --timeout=180s || { \
			echo ""; \
			echo "=== $$svc không rollout xong, lý do: ==="; \
			kubectl -n $(NAMESPACE) describe deployment/$$svc | sed -n '/Conditions/,/Events/p'; \
			kubectl -n $(NAMESPACE) get pods -l app=$$svc \
				-o custom-columns='POD:.metadata.name,STATUS:.status.phase,LÝDO:.status.containerStatuses[0].state.waiting.reason'; \
			echo ""; \
			echo "ImagePullBackOff nghĩa là image cho commit này chưa có trên vCR —"; \
			echo "kiểm tra job build-and-push trên GitHub Actions."; \
			exit 1; }; \
	done
	$(MAKE) k8s-verify

# Kiểm tra từng service sau khi deploy: pod chạy image nào, và có thực sự nói
# chuyện được với database không.
k8s-verify:
	@echo ""
	@echo "=== Image đang chạy ==="
	@kubectl -n $(NAMESPACE) get deploy \
		-o custom-columns='SERVICE:.metadata.name,IMAGE:.spec.template.spec.containers[0].image'
	@echo ""
	@echo "=== Kết nối database của từng service ==="
	@for svc in $(SERVICES); do \
		printf "  %-28s " "$$svc"; \
		kubectl -n $(NAMESPACE) exec deploy/$$svc -- \
			python -c "import urllib.request,json; \
			print(json.load(urllib.request.urlopen('http://localhost:8080/health/db'))['database'])" \
			2>/dev/null || echo "không gọi được"; \
	done


# Mở cả 5 service ra localhost cùng lúc. Các Service đều là ClusterIP (cố ý —
# API chưa có xác thực nên không phơi ra Internet), nên đây là đường vào duy nhất.
# Cổng trùng với quy ước chạy local để cùng một lệnh curl dùng được ở cả hai nơi.
PF_BASE ?= 8081

port-forward:
	@command -v kubectl >/dev/null || { echo "ERROR: không có kubectl"; exit 1; }
	@kubectl -n $(NAMESPACE) get deploy >/dev/null 2>&1 || { \
		echo "ERROR: không kết nối được cluster. Đặt KUBECONFIG trước:"; \
		echo "  export KUBECONFIG=~/.kube/vks-finance-demo.yaml"; exit 1; }
	@trap 'echo ""; echo "Đang đóng..."; kill 0' INT TERM EXIT; \
	i=0; \
	for svc in $(SERVICES); do \
		port=$$(($(PF_BASE) + $$i)); \
		kubectl -n $(NAMESPACE) port-forward svc/$$svc $$port:80 >/dev/null 2>&1 & \
		i=$$((i + 1)); \
	done; \
	sleep 4; \
	echo ""; \
	i=0; \
	for svc in $(SERVICES); do \
		port=$$(($(PF_BASE) + $$i)); \
		state=$$(curl -s --max-time 3 localhost:$$port/health/db 2>/dev/null \
		         | sed -n 's/.*"database":"\([a-z]*\)".*/\1/p'); \
		printf "  %-26s http://localhost:%s/docs   db=%s\n" \
			"$$svc" "$$port" "$${state:-không phản hồi}"; \
		i=$$((i + 1)); \
	done; \
	echo ""; \
	echo "  Ctrl+C để đóng tất cả."; \
	wait

# --- Đóng gói & triển khai --------------------------------------------------
build: sync-common
	@for svc in $(SERVICES); do \
		echo "==> build $(REGISTRY)/$$svc:$(TAG)"; \
		docker build -t $(REGISTRY)/$$svc:$(TAG) -t $(REGISTRY)/$$svc:latest services/$$svc || exit 1; \
	done

k8s-secret:
	@test -n "$(VCR_USERNAME)" || { echo "ERROR: chưa đặt VCR_USERNAME"; exit 1; }
	@test -n "$(VCR_PASSWORD)" || { echo "ERROR: chưa đặt VCR_PASSWORD"; exit 1; }
	kubectl apply -f k8s/namespace.yaml
	kubectl -n $(NAMESPACE) create secret docker-registry $(PULL_SECRET) \
		--docker-server=$(REGISTRY_HOST) \
		--docker-username=$(VCR_USERNAME) \
		--docker-password=$(VCR_PASSWORD) \
		--dry-run=client -o yaml | kubectl apply -f -

k8s-db-secret:
	@test -n "$(DATABASE_URL)" || { echo "ERROR: chưa đặt DATABASE_URL"; exit 1; }
	kubectl apply -f k8s/namespace.yaml
	kubectl -n $(NAMESPACE) create secret generic guardian-db \
		--from-literal=DATABASE_URL="$(DATABASE_URL)" \
		--dry-run=client -o yaml | kubectl apply -f -
	@echo "Đã tạo secret 'guardian-db'."

k8s-apply:
	kubectl apply -f k8s/namespace.yaml
	$(MAKE) k8s-secret
	$(MAKE) k8s-db-secret
	kubectl apply -f k8s/
