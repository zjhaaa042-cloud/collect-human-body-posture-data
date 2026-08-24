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

            points = np.asarray(points, dtype='<f4')
            if colors is None:
                f.write(points.tobytes())
                return

            colors = np.asarray(colors, dtype=np.uint8)
            vertex_dtype = np.dtype([
                ('x', '<f4'),
                ('y', '<f4'),
                ('z', '<f4'),
                ('red', 'u1'),
                ('green', 'u1'),
                ('blue', 'u1'),
            ])
            vertices = np.empty(len(points), dtype=vertex_dtype)
            vertices['x'] = points[:, 0]
            vertices['y'] = points[:, 1]
            vertices['z'] = points[:, 2]
            vertices['red'] = colors[:, 0]
            vertices['green'] = colors[:, 1]
            vertices['blue'] = colors[:, 2]
            f.write(vertices.tobytes())

    @staticmethod
    def load(filepath: str):
        try:
            with open(filepath, 'rb') as f:
                file_format = None
                vertex_count = 0
                current_element = None
                vertex_properties = []

                while True:
                    raw_line = f.readline()
                    if not raw_line:
                        raise ValueError("PLY header is incomplete")
                    line = raw_line.decode('ascii').strip()
                    parts = line.split()
                    if parts[:1] == ["format"]:
                        file_format = parts[1]
                    elif parts[:1] == ["element"]:
                        current_element = parts[1]
                        if current_element == "vertex":
                            vertex_count = int(parts[2])
                    elif parts[:1] == ["property"] and current_element == "vertex":
                        if len(parts) != 3 or parts[1] == "list":
                            raise ValueError("Unsupported vertex property declaration")
                        vertex_properties.append((parts[2], parts[1]))
                    if line == "end_header":
                        break

                names = [name for name, _ in vertex_properties]
                for required in ("x", "y", "z"):
                    if required not in names:
                        raise ValueError(f"PLY is missing vertex property: {required}")

                if file_format == "ascii":
                    rows = []
                    for _ in range(vertex_count):
                        raw_row = f.readline()
                        if not raw_row:
                            raise ValueError("PLY vertex data is incomplete")
                        rows.append(raw_row.decode('ascii').split())
                    values = np.asarray(rows, dtype=np.float64)
                    points = values[:, [names.index("x"), names.index("y"), names.index("z")]].astype(np.float32)
                    colors = None
                    if all(name in names for name in ("red", "green", "blue")):
                        colors = values[:, [names.index("red"), names.index("green"), names.index("blue")]].astype(np.uint8)
                    return points, colors

                if file_format != "binary_little_endian":
                    raise ValueError(f"Unsupported PLY format: {file_format}")

                numpy_types = {
                    "char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1",
                    "short": "<i2", "int16": "<i2", "ushort": "<u2", "uint16": "<u2",
                    "int": "<i4", "int32": "<i4", "uint": "<u4", "uint32": "<u4",
                    "float": "<f4", "float32": "<f4", "double": "<f8", "float64": "<f8",
                }
                try:
                    vertex_dtype = np.dtype([(name, numpy_types[data_type]) for name, data_type in vertex_properties])
                except KeyError as exc:
                    raise ValueError(f"Unsupported PLY property type: {exc.args[0]}") from exc
                vertices = np.fromfile(f, dtype=vertex_dtype, count=vertex_count)
                if len(vertices) != vertex_count:
                    raise ValueError("PLY vertex data is incomplete")
                points = np.column_stack((vertices["x"], vertices["y"], vertices["z"])).astype(np.float32)
                colors = None
                if all(name in names for name in ("red", "green", "blue")):
                    colors = np.column_stack((vertices["red"], vertices["green"], vertices["blue"])).astype(np.uint8)
                return points, colors
        except Exception as e:
            logger.error(f"Failed to load PLY file: {e}")
            return None, None
