"""近义模块名：分侧凸轮补偿节拍规划。"""

from .springdesk import CamScheduleError as CamScheduleError
from .springdesk import plan_cam_compensation as plan_cam_compensation
from .springdesk import plan_cam_order as plan_cam_order

__all__ = ["CamScheduleError", "plan_cam_order", "plan_cam_compensation"]
