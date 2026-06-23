import numpy as np
from typing import Optional
from loguru import logger


class PLYWriter:
    @staticmethod
    def save(filepath: str, points: np.ndarray, colors: Optional[np.ndarray] = None, binary: bool = False):
        try:
            valid_mask = np.any(np.abs(points) > 1e-6, axis=1)
            valid_points = points[valid_mask]
            valid_colors = colors[valid_mask] if colors is not None else None

            if binary:
                PLYWriter._save_binary(filepath, valid_points, valid_colors)
            else:
                PLYWriter._save_ascii(filepath, valid_points, valid_colors)

            logger.info(f"Saved PLY file: {filepath} ({len(valid_points)} points)")
        except Exception as e:
            logger.error(f"Failed to save PLY file: {e}")
            raise

    @staticmethod
    def _save_ascii(filepath: str, points: np.ndarray, colors: Optional[np.ndarray] = None):
        with open(filepath, 'w') as f:
            f.write("ply\n")
            f.write("format ascii 1.0\n")
            f.write(f"element vertex {len(points)}\n")
            f.write("property float x\n")
            f.write("property float y\n")
            f.write("property float z\n")
            if colors is not None:
                f.write("property uchar red\n")
                f.write("property uchar green\n")
                f.write("property uchar blue\n")
            f.write("end_header\n")

            for i in range(len(points)):
                line = f"{points[i, 0]:.3f} {points[i, 1]:.3f} {points[i, 2]:.3f}"
                if colors is not None:
                    line += f" {colors[i, 0]} {colors[i, 1]} {colors[i, 2]}"
                f.write(line + "\n")

    @staticmethod
    def _save_binary(filepath: str, points: np.ndarray, colors: Optional[np.ndarray] = None):
        with open(filepath, 'wb') as f:
            header = "ply\n"
            header += "format binary_little_endian 1.0\n"
            header += f"element vertex {len(points)}\n"
            header += "property float x\n"
            header += "property float y\n"
            header += "property float z\n"
            if colors is not None:
                header += "property uchar red\n"
                header += "property uchar green\n"
                header += "property uchar blue\n"
            header += "end_header\n"
            f.write(header.encode('utf-8'))

            for i in range(len(points)):
                f.write(points[i].tobytes())
                if colors is not None:
                    f.write(colors[i].tobytes())

    @staticmethod
    def load(filepath: str):
        try:
            with open(filepath, 'r') as f:
                header_lines = []
                vertex_count = 0
                has_color = False

                while True:
                    line = f.readline().strip()
                    header_lines.append(line)
                    if line.startswith("element vertex"):
                        vertex_count = int(line.split()[-1])
                    if "property uchar red" in line:
                        has_color = True
                    if line == "end_header":
                        break

                points = np.zeros((vertex_count, 3), dtype=np.float32)
                colors = np.zeros((vertex_count, 3), dtype=np.uint8) if has_color else None

                for i in range(vertex_count):
                    values = f.readline().strip().split()
                    points[i] = [float(v) for v in values[:3]]
                    if has_color:
                        colors[i] = [int(v) for v in values[3:6]]

                return points, colors
        except Exception as e:
            logger.error(f"Failed to load PLY file: {e}")
            return None, None
