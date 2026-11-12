# Use OpenTofu with: make plan TF=tofu
TF ?= terraform

export TF_CLI_CONFIG_FILE := $(CURDIR)/.provider/dev.tfrc

.PHONY: help provider fmt validate plan apply destroy output clean test-tools

help:
	@echo "make provider   build the SimpleVM provider from source (once, or to update)"
	@echo "make validate   check formatting and configuration"
	@echo "make plan       show what would change"
	@echo "make apply      create/update the research environment VM"
	@echo "make output     show the research environment URL and other outputs"
	@echo "make destroy    delete the VM and its volume"
	@echo "make test-tools run the tests of tools/simplevm_api.py (API debug client)"
	@echo "Set TF=tofu to use OpenTofu instead of Terraform."

.provider/dev.tfrc:
	./scripts/build-provider.sh

provider:
	./scripts/build-provider.sh

fmt:
	$(TF) fmt -recursive

validate: .provider/dev.tfrc
	$(TF) fmt -check -recursive
	$(TF) validate

plan: .provider/dev.tfrc
	$(TF) plan

apply: .provider/dev.tfrc
	$(TF) apply

output: .provider/dev.tfrc
	$(TF) output

destroy: .provider/dev.tfrc
	$(TF) destroy

test-tools:
	python3 -m unittest discover -s tools

clean:
	rm -rf .provider
