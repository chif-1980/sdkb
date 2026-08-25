"""
RapidOCR 解析器 - 纯OCR文字识别

使用 RapidOCR (PP-OCRv5) 进行文字识别
"""

import os
import math
import multiprocessing
import tempfile
import threading
import time
from pathlib import Path

import fitz
import numpy as np
from PIL import Image
from rapidocr import EngineType, LangDet, LangRec, ModelType, OCRVersion, RapidOCR

from yuxi.knowledge.parser.base import BaseDocumentProcessor, OCRException
from yuxi.utils import logger


DEFAULT_MAX_RENDER_PIXELS = 20_000_000
DEFAULT_MAX_RENDER_DIMENSION = 8192
DEFAULT_MAX_SOURCE_PIXELS = 600_000_000
DEFAULT_PDF_NATIVE_TEXT_MIN_CHARS = 80


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _bounded_render_scale(
    width: float,
    height: float,
    scale_x: float,
    scale_y: float,
    *,
    max_pixels: int,
    max_dimension: int,
) -> tuple[float, float]:
    output_width = max(width * scale_x, 1)
    output_height = max(height * scale_y, 1)
    shrink = min(
        1.0,
        max_dimension / output_width,
        max_dimension / output_height,
        math.sqrt(max_pixels / (output_width * output_height)),
    )
    return scale_x * shrink, scale_y * shrink


def _rapid_ocr_process_entry(
    connection,
    file_path: str,
    params: dict | None,
    det_box_thresh: float,
    result_path: str,
) -> None:
    """Run native OCR outside the API process so native crashes are contained."""
    try:
        parser = RapidOCRParser(det_box_thresh=det_box_thresh)
        result = parser._process_file_inline(file_path, params)
        with open(result_path, "w", encoding="utf-8") as result_file:
            result_file.write(result)
        connection.send(("ok", None))
    except BaseException as exc:  # noqa: BLE001
        connection.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        connection.close()


class RapidOCRParser(BaseDocumentProcessor):
    """RapidOCR 解析器 - 使用 ONNX 模型进行文字识别"""

    def __init__(self, det_box_thresh: float = 0.3):
        self.ocr = None
        self.det_box_thresh = det_box_thresh
        self._model_lock = threading.Lock()
        self._process_lock = threading.Lock()

    def get_service_name(self) -> str:
        return "rapid_ocr"

    def get_supported_extensions(self) -> list[str]:
        return [".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"]

    def _get_model_params(self) -> dict[str, object]:
        return {
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Det.lang_type": LangDet.CH,
            "Det.model_type": ModelType.MOBILE,
            "Det.ocr_version": OCRVersion.PPOCRV5,
            "Det.box_thresh": self.det_box_thresh,
            "Cls.engine_type": EngineType.ONNXRUNTIME,
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Rec.lang_type": LangRec.CH,
            "Rec.model_type": ModelType.MOBILE,
            "Rec.ocr_version": OCRVersion.PPOCRV5,
        }

    def check_health(self) -> dict:
        """检查 RapidOCR 模型是否可用"""
        try:
            test_ocr = RapidOCR(params=self._get_model_params())
            del test_ocr
            return {
                "status": "healthy",
                "message": "RapidOCR PP-OCRv5 模型可用",
                "details": {"ocr_version": "PP-OCRv5", "engine": "onnxruntime"},
            }
        except Exception as e:
            return {"status": "error", "message": f"模型加载失败: {str(e)}", "details": {"error": str(e)}}

    def _load_model(self):
        """延迟加载 OCR 模型"""
        if self.ocr is not None:
            return

        with self._model_lock:
            if self.ocr is not None:
                return

            logger.info("加载 RapidOCR 模型...")

            try:
                self.ocr = RapidOCR(params=self._get_model_params())
                logger.info(f"RapidOCR PP-OCRv5 模型加载成功 (det_box_thresh={self.det_box_thresh})")
            except Exception as e:
                raise OCRException(f"RapidOCR模型加载失败: {str(e)}", self.get_service_name(), "load_failed")

    def _render_limits(self, params: dict | None = None) -> tuple[int, int, int]:
        params = params or {}
        return (
            _positive_int(
                params.get("max_image_pixels", os.getenv("RAPID_OCR_MAX_IMAGE_PIXELS")),
                DEFAULT_MAX_RENDER_PIXELS,
            ),
            _positive_int(
                params.get("max_image_dimension", os.getenv("RAPID_OCR_MAX_IMAGE_DIMENSION")),
                DEFAULT_MAX_RENDER_DIMENSION,
            ),
            _positive_int(
                params.get("max_source_pixels", os.getenv("RAPID_OCR_MAX_SOURCE_PIXELS")),
                DEFAULT_MAX_SOURCE_PIXELS,
            ),
        )

    def _prepare_image_path(self, image_path: str, params: dict | None = None) -> str | None:
        """Render oversized images to a bounded temporary PNG without Pillow decoding."""

        max_pixels, max_dimension, max_source_pixels = self._render_limits(params)
        try:
            image_doc = fitz.open(image_path)
        except Exception as exc:
            raise OCRException(
                f"无法读取图像尺寸: {exc}",
                self.get_service_name(),
                "invalid_image",
            ) from exc

        try:
            if image_doc.page_count < 1:
                raise OCRException("图像中没有可处理页面", self.get_service_name(), "invalid_image")
            page = image_doc[0]
            width = float(page.rect.width)
            height = float(page.rect.height)
            source_pixels = width * height
            if source_pixels > max_source_pixels:
                raise OCRException(
                    f"图像像素数 {int(source_pixels)} 超过安全上限 {max_source_pixels}",
                    self.get_service_name(),
                    "image_too_large",
                )
            scale_x, scale_y = _bounded_render_scale(
                width,
                height,
                1.0,
                1.0,
                max_pixels=max_pixels,
                max_dimension=max_dimension,
            )
            if scale_x >= 0.999 and scale_y >= 0.999:
                return None

            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
                resized_path = temp_file.name
            try:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale_x, scale_y), alpha=False)
                pixmap.save(resized_path)
            except Exception:
                try:
                    os.unlink(resized_path)
                except FileNotFoundError:
                    pass
                raise
            logger.info(
                "RapidOCR 图像安全缩放: {}x{} -> {}x{}",
                int(width),
                int(height),
                pixmap.width,
                pixmap.height,
            )
            return resized_path
        except OCRException:
            raise
        except Exception as exc:
            raise OCRException(
                f"超大图像缩放失败: {exc}",
                self.get_service_name(),
                "image_resize_failed",
            ) from exc
        finally:
            image_doc.close()

    def process_image(self, image, params: dict | None = None) -> str:
        """
        处理单张图像并提取文本

        Args:
            image: 图像数据,支持:
                  - str: 图像文件路径
                  - PIL.Image: PIL图像对象
                  - numpy.ndarray: numpy图像数组
            params: 处理参数 (当前未使用)

        Returns:
            str: 提取的文本内容
        """
        cleanup_paths: list[str] = []
        try:
            # 处理不同类型的输入
            if isinstance(image, str):
                image_path = image
            else:
                # 创建临时文件
                image_path = self._create_temp_image_file(image)
                cleanup_paths.append(image_path)

            resized_path = self._prepare_image_path(image_path, params)
            if resized_path is not None:
                cleanup_paths.append(resized_path)
                ocr_path = resized_path
            else:
                ocr_path = image_path

            self._load_model()
            start_time = time.time()
            # ONNXRuntime-backed OCR instances are shared by the factory;
            # serialize inference to avoid native runtime races.
            with self._process_lock:
                result = self.ocr(ocr_path)
            processing_time = time.time() - start_time

            if result.txts:
                text = "\n".join(result.txts)
                logger.info(
                    f"RapidOCR 成功: {os.path.basename(image_path) if isinstance(image, str) else 'temp_image'}"
                    f" ({processing_time:.2f}s)"
                )
                return text
            logger.warning(f"RapidOCR 未识别到文本: {image_path}")
            return ""
        except OCRException:
            raise
        except Exception as e:
            error_msg = f"图像OCR处理失败: {str(e)}"
            logger.error(error_msg)
            raise OCRException(error_msg, self.get_service_name(), "processing_failed")
        finally:
            for cleanup_path in cleanup_paths:
                try:
                    os.unlink(cleanup_path)
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    logger.warning(f"临时文件清理失败: {cleanup_path} - {exc}")

    def _create_temp_image_file(self, image) -> str:
        """将图像数据保存为临时文件"""
        try:
            # 使用系统临时目录
            with tempfile.NamedTemporaryFile(mode="wb", suffix=".png", delete=False) as tmp_file:
                temp_path = tmp_file.name

                if isinstance(image, Image.Image):
                    image.save(temp_path)
                elif isinstance(image, np.ndarray):
                    Image.fromarray(image).save(temp_path)
                else:
                    raise ValueError("不支持的图像类型,必须是 PIL.Image 或 numpy.ndarray")

                return temp_path

        except Exception as e:
            raise OCRException(f"临时图像文件创建失败: {str(e)}", self.get_service_name(), "temp_file_error")

    def process_pdf(self, pdf_path: str, params: dict | None = None) -> str:
        """
        处理 PDF 文件并提取文本 (流式处理,避免内存占用)

        Args:
            pdf_path: PDF 文件路径
            params: 处理参数
                - zoom_x: 横向缩放 (默认 2)
                - zoom_y: 纵向缩放 (默认 2)

        Returns:
            str: 提取的文本
        """
        if not os.path.exists(pdf_path):
            raise OCRException(f"PDF 文件不存在: {pdf_path}", self.get_service_name(), "file_not_found")

        params = params or {}
        zoom_x = params.get("zoom_x", 2)
        zoom_y = params.get("zoom_y", 2)
        max_pixels, max_dimension, _ = self._render_limits(params)
        native_text_min_chars = _positive_int(
            params.get("pdf_native_text_min_chars", os.getenv("RAPID_OCR_PDF_NATIVE_TEXT_MIN_CHARS")),
            DEFAULT_PDF_NATIVE_TEXT_MIN_CHARS,
        )

        try:
            with fitz.open(pdf_path) as pdf_doc:
                total_pages = pdf_doc.page_count
                native_pages = [page.get_text("text").strip() for page in pdf_doc]
                native_text = "\n\n".join(page_text for page_text in native_pages if page_text).strip()
                if len(native_text) >= native_text_min_chars:
                    logger.info(
                        f"PDF 原生文字提取完成: {os.path.basename(pdf_path)} - {len(native_text)} 字符"
                    )
                    return native_text

                all_text = []
                logger.info(f"开始处理 PDF OCR: {os.path.basename(pdf_path)} ({total_pages} 页)")

                # 流式处理每一页,避免一次性加载所有图片到内存
                for page_num in range(total_pages):
                    page = pdf_doc[page_num]

                    render_x, render_y = _bounded_render_scale(
                        float(page.rect.width),
                        float(page.rect.height),
                        float(zoom_x),
                        float(zoom_y),
                        max_pixels=max_pixels,
                        max_dimension=max_dimension,
                    )
                    pix = page.get_pixmap(matrix=fitz.Matrix(render_x, render_y), alpha=False)
                    img_pil = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

                    # 立即处理,不保存到列表
                    text = self.process_image(img_pil, params)
                    all_text.append(text)

                    if (page_num + 1) % 10 == 0:
                        logger.info(f"已处理 {page_num + 1}/{total_pages} 页")

                result_text = "\n\n".join(all_text)
                logger.info(f"PDF OCR 完成: {os.path.basename(pdf_path)} - {len(result_text)} 字符")
                return result_text

        except OCRException:
            raise
        except Exception as e:
            error_msg = f"PDF OCR 处理失败: {str(e)}"
            logger.error(error_msg)
            raise OCRException(error_msg, self.get_service_name(), "pdf_processing_failed")

    def _process_file_inline(self, file_path: str, params: dict | None = None) -> str:
        """
        处理文件 (PDF 或图像)

        Args:
            file_path: 文件路径
            params: 处理参数

        Returns:
            str: 提取的文本
        """
        file_ext = Path(file_path).suffix.lower()

        if not self.supports_file_type(file_ext):
            raise OCRException(f"不支持的文件类型: {file_ext}", self.get_service_name(), "unsupported_file_type")

        if file_ext == ".pdf":
            return self.process_pdf(file_path, params)
        else:
            return self.process_image(file_path, params)

    def process_file(self, file_path: str, params: dict | None = None) -> str:
        """Run OCR in a child process so ONNXRuntime failures stay isolated."""
        timeout_seconds = float(os.getenv("RAPID_OCR_PROCESS_TIMEOUT_SECONDS", "300"))
        context = multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe(duplex=False)
        result_handle = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
        result_path = result_handle.name
        result_handle.close()
        process = context.Process(
            target=_rapid_ocr_process_entry,
            args=(child_connection, file_path, params, self.det_box_thresh, result_path),
            daemon=True,
        )
        process.start()
        child_connection.close()
        try:
            process.join(timeout_seconds)
            if process.is_alive():
                process.terminate()
                process.join(5)
                raise OCRException(
                    f"OCR 子进程超过 {timeout_seconds:g} 秒未完成",
                    self.get_service_name(),
                    "processing_timeout",
                )
            if parent_connection.poll():
                try:
                    status, payload = parent_connection.recv()
                except EOFError as exc:
                    raise OCRException(
                        f"OCR 子进程连接提前关闭 (exitcode={process.exitcode})",
                        self.get_service_name(),
                        "process_crashed",
                    ) from exc
                if status == "ok":
                    with open(result_path, encoding="utf-8") as result_file:
                        return result_file.read()
                raise OCRException(f"OCR 子进程失败: {payload}", self.get_service_name(), "processing_failed")
            raise OCRException(
                f"OCR 子进程异常退出 (exitcode={process.exitcode})",
                self.get_service_name(),
                "process_crashed",
            )
        finally:
            parent_connection.close()
            try:
                os.unlink(result_path)
            except FileNotFoundError:
                pass
