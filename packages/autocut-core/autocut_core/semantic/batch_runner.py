"""
Batch runner 重导出模块

向后兼容：重导出 ac_auto_cut.semantic.batch_runner
使用运行时导入避免共享包对项目包的直接依赖
"""
import sys


def _get_batch_runner():
    """运行时导入 batch_runner，避免模块级依赖"""
    try:
        from ac_auto_cut.semantic import batch_runner as _impl
        return _impl
    except ImportError:
        raise ImportError(
            "batch_runner requires ac_auto_cut package. "
            "Install it with: pip install ac-auto-cut"
        )


def __getattr__(name):
    """延迟属性访问，转发到 ac_auto_cut.semantic.batch_runner"""
    _impl = _get_batch_runner()
    return getattr(_impl, name)


# 提供模块级访问
def run_batch(*args, **kwargs):
    return _get_batch_runner().run_batch(*args, **kwargs)


def run_batch_stream(*args, **kwargs):
    return _get_batch_runner().run_batch_stream(*args, **kwargs)
