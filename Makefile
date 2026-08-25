# 本地开发常用命令。CI 跑的是同一套，避免"本地过了 CI 挂"。
.DEFAULT_GOAL := help
PY := .venv/bin/python

.PHONY: help install test cov lint fmt typecheck check doctor clean

help:            ## 显示本帮助
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:         ## 建虚拟环境并装依赖（含 dev）
	python3 -m venv .venv
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e ".[dev]"

test:            ## 跑测试
	TZ=Asia/Shanghai $(PY) -m pytest -q

cov:             ## 跑测试并输出覆盖率
	TZ=Asia/Shanghai $(PY) -m pytest -q --cov=astock --cov-report=term-missing

lint:            ## 静态检查
	$(PY) -m ruff check astock tests

fmt:             ## 自动修复可修的静态问题
	$(PY) -m ruff check --fix astock tests

typecheck:       ## 类型检查（core/runtime 为严格模式）
	$(PY) -m mypy astock

check: lint test ## 提交前跑这个

doctor:          ## 环境体检：时钟 / 工作区 / 13 个账户账本
	$(PY) -m astock.cli.main doctor

clean:           ## 清掉缓存与覆盖率产物
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
