"""FIBO 闭包工具包（data/fibo）。

本目录为**构建期**工具与锁定资产：
- 冒烟/验证脚本（smoke_fibo / check_iris / probe_annotations / validate_alignments）；
- 权威 IRI 注册表（iri_registry.json）；
- 概念索引 AOT 构建器（build_concept_index.py，ADR-0032 M1）。

纪律：本包内模块被 tests 导入时不得触发 rdflib 加载（rdflib 仅构建期用，
按函数延迟 import）。
"""
