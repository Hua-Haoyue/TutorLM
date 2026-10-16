"""受限 TeX 编译适配器：**只经受信 executor 端口**，无执行器即不可用。

**绝不做**：

- 同 App subprocess fallback（清 env **不等于**安全沙箱，见专文 §0/§2.3）；
- 网络 fetch、任意 shell / 用户代码、读取卷外文件（这些属于沙箱创建方的职责）。

本类只做编排与产物**校验**：把声明式 ``CompileRequest`` 交给 executor，
并对返回的 PDF 做 size + magic 校验（产物同样不可信）。无 executor 时
``available()`` 为 ``False``，上层返回 ``503 tex_compile_unavailable``，**不入队**。
"""

from __future__ import annotations

from backend.application.format.contracts import (
    CompileRequest,
    CompileResult,
    TexCompileExecutor,
)
from backend.application.format.errors import TexCompileUnavailable
from backend.domain.hashing import content_hash

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
_PDF_MAGIC = b"%PDF-"


class TexCompiler:
    """对 ``TexCompileExecutor`` 端口的薄封装 + 产物校验。"""

    def __init__(self, executor: TexCompileExecutor | None = None) -> None:
        self._executor = executor

    def available(self) -> bool:
        if self._executor is None:
            return False
        try:
            return bool(self._executor.available())
        except Exception:
            return False

    def engine_version(self) -> str | None:
        if not self.available():
            return None
        try:
            return self._executor.engine_version()
        except Exception:
            return None

    def compile(self, request: CompileRequest) -> CompileResult:
        if not self.available():
            # 直接不可用：不入队、不回退 subprocess。
            raise TexCompileUnavailable(
                "无受信 TeX 编译执行器：不回退同 App subprocess",
                details={"engine": request.engine},
            )
        result = self._executor.compile(request)
        return _validate_artifact(result)


def _validate_artifact(result: CompileResult) -> CompileResult:
    """产物校验：只接受期望的 PDF，size 与 magic 都要过。"""

    if result.status != "completed" or result.artifact_bytes is None:
        return result
    payload = result.artifact_bytes
    if len(payload) > MAX_ARTIFACT_BYTES:
        return CompileResult(
            status="failed",
            engine_version=result.engine_version,
            log=result.log,
            error_code="artifact_too_large",
            error_message="编译产物超过大小上限",
        )
    if not payload.startswith(_PDF_MAGIC):
        return CompileResult(
            status="failed",
            engine_version=result.engine_version,
            log=result.log,
            error_code="artifact_not_pdf",
            error_message="编译产物不是 PDF（magic 校验失败）",
        )
    return CompileResult(
        status="completed",
        artifact_bytes=payload,
        engine_version=result.engine_version,
        log=result.log,
        source_map=result.source_map,
        error_code=None,
        error_message=None,
    )


def artifact_hash(payload: bytes) -> str:
    """产物摘要：委托 ``domain.hashing.content_hash``（裸十六进制）。"""

    return content_hash(payload)
