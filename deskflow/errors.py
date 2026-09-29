"""库对外抛出的异常类型。"""


class DeskflowError(Exception):
    """所有库级错误的基类。"""


class BatchNotFoundError(DeskflowError):
    """按标识找不到对应批次。"""


class UndoWindowExpiredError(DeskflowError):
    """批次撤销窗口已过（提交后满 300 秒）。"""
