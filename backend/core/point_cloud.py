import numpy as np
from typing import Optional, Tuple
from loguru import logger


class PointCloudProcessor:
    def __init__(self):
        pass

    @staticmethod
    def depth_to_point_cloud(depth: np.ndarray, intrinsics: dict, scale: float = 1.0) -> np.ndarray:
        try:
            h, w = depth.shape
            fx = intrinsics.get('fx', 500.0)
            fy = intrinsics.get('fy', 500.0)
            cx = intrinsics.get('cx', w / 2)
            cy = intrinsics.get('cy', h / 2)

            v, u = np.mgrid[0:h, 0:w]
            z = depth.astype(np.float32) * scale
            x = (u - cx) * z / fx
            y = (v - cy) * z / fy

            points = np.stack([x, y, z], axis=-1).reshape(-1, 3)
            return points
        except Exception as e:
            logger.error(f"Failed to convert depth to point cloud: {e}")
            return np.array([])

    @staticmethod
    def filter_valid_points(points: np.ndarray, colors: Optional[np.ndarray] = None,
                           min_depth: float = 0.0, max_depth: float = 10000.0) -> Tuple[np.ndarray, Optional[np.ndarray]]:
        try:
            z = points[:, 2]
            valid_mask = (z > min_depth) & (z < max_depth) & np.any(np.abs(points) > 1e-6, axis=1)

            valid_points = points[valid_mask]
            valid_colors = colors[valid_mask] if colors is not None else None

            return valid_points, valid_colors
        except Exception as e:
            logger.error(f"Failed to filter points: {e}")
            return points, colors

    @staticmethod
    def downsample_point_cloud(points: np.ndarray, colors: Optional[np.ndarray] = None,
                              voxel_size: float = 5.0) -> Tuple[np.ndarray, Optional[np.ndarray]]:
        try:
            min_coords = np.min(points, axis=0)
            voxel_indices = np.floor((points - min_coords) / voxel_size).astype(int)

            voxel_dict = {}
            for i in range(len(points)):
                key = tuple(voxel_indices[i])
                if key not in voxel_dict:
                    voxel_dict[key] = []
                voxel_dict[key].append(i)

            downsampled_points = []
            downsampled_colors = []

            for indices in voxel_dict.values():
                idx = indices[0]
                downsampled_points.append(points[idx])
                if colors is not None:
                    downsampled_colors.append(colors[idx])

            result_points = np.array(downsampled_points)
            result_colors = np.array(downsampled_colors) if colors is not None else None

            return result_points, result_colors
        except Exception as e:
            logger.error(f"Failed to downsample point cloud: {e}")
            return points, colors

    @staticmethod
    def save_ply(filepath: str, points: np.ndarray, colors: Optional[np.ndarray] = None):
        try:
            valid_points, valid_colors = PointCloudProcessor.filter_valid_points(points, colors)

            with open(filepath, 'w') as f:
                f.write("ply\n")
                f.write("format ascii 1.0\n")
                f.write(f"element vertex {len(valid_points)}\n")
                f.write("property float x\n")
                f.write("property float y\n")
                f.write("property float z\n")
                if valid_colors is not None:
                    f.write("property uchar red\n")
                    f.write("property uchar green\n")
                    f.write("property uchar blue\n")
                f.write("end_header\n")

                for i in range(len(valid_points)):
                    line = f"{valid_points[i, 0]:.3f} {valid_points[i, 1]:.3f} {valid_points[i, 2]:.3f}"
                    if valid_colors is not None:
                        line += f" {valid_colors[i, 0]} {valid_colors[i, 1]} {valid_colors[i, 2]}"
                    f.write(line + "\n")

            logger.info(f"Saved point cloud to {filepath}: {len(valid_points)} points")
        except Exception as e:
            logger.error(f"Failed to save PLY file: {e}")
            raise
