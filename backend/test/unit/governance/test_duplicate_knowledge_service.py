from types import SimpleNamespace

import pytest

from yuxi.governance.duplicate_knowledge_service import DuplicateKnowledgeService, ParsedFragment


def matches(source, target):
    relation = SimpleNamespace(
        relation_id="relation", source_version_id="source", target_version_id="target", relation_type="OVERLAP"
    )
    versions = {key: SimpleNamespace(yuxi_file_id=key) for key in ("source", "target")}
    chunks = {
        "s": ParsedFragment("s", "source", 0, source),
        "t": ParsedFragment("t", "target", 0, target),
    }
    return DuplicateKnowledgeService._match_fragments(relation, versions, chunks)


@pytest.mark.parametrize("ocr", ["", "\n图片文字：D D", "\n图片文字：●"])
def test_image_paths_and_icon_ocr_do_not_create_text_duplicates(ocr):
    source = (
        '![image_1789460920883440.png](/minio/public/kb/kb-images/1789460920883440.png '
        '"/minio/public/kb/kb-images/1789460920883440.webp")'
    )
    target = source.replace("1789460920883440", "1789457620983428")
    assert matches(source + ocr, target + ocr) == []


def test_real_ocr_evidence_survives_without_image_url_similarity():
    text = "系统支持知识问答、资料采集、投诉处理和人工客服协同，并提供完整的业务办理记录。"
    result = matches(
        "![chart](/minio/public/kb/kb-images/a.png)\n图片文字：" + text,
        "![screenshot](/minio/public/kb/kb-images/b.png)\n图片文字：" + text,
    )
    assert len(result) == 1
    assert result[0]["similarity"] == 1
    assert result[0]["source_overlap_excerpt"] == text
    assert result[0]["target_overlap_excerpt"] == text


def test_matching_file_paths_do_not_override_unrelated_body_text():
    image = "![image](/minio/public/kb/kb-images/1789460920883440.png)" * 8
    assert matches(
        image + "\n销售人员使用业务系统办理客户合同审批并保存订单资料，方便查询客户购买情况。",
        image + "\n服务器网络连接异常时检查路由配置和防火墙端口，重启计算机后继续排查硬件故障。",
    ) == []
