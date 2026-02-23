# µReticulum Minimal MessagePack
# Implements the subset needed for LXMF: float64, bytes, str, list, dict, None, int, bool
# Wire-compatible with reference umsgpack / msgpack

import struct


def packb(obj):
    """Serialize object to msgpack bytes"""
    if obj is None:
        return b'\xc0'

    elif obj is True:
        return b'\xc3'

    elif obj is False:
        return b'\xc2'

    elif isinstance(obj, int):
        if 0 <= obj <= 0x7f:
            return bytes([obj])
        elif -32 <= obj < 0:
            return struct.pack("b", obj)
        elif 0 <= obj <= 0xff:
            return b'\xcc' + bytes([obj])
        elif 0 <= obj <= 0xffff:
            return b'\xcd' + struct.pack(">H", obj)
        elif 0 <= obj <= 0xffffffff:
            return b'\xce' + struct.pack(">I", obj)
        elif 0 <= obj <= 0xffffffffffffffff:
            return b'\xcf' + struct.pack(">Q", obj)
        elif -128 <= obj < 0:
            return b'\xd0' + struct.pack("b", obj)
        elif -32768 <= obj < 0:
            return b'\xd1' + struct.pack(">h", obj)
        elif -2147483648 <= obj < 0:
            return b'\xd2' + struct.pack(">i", obj)
        else:
            return b'\xd3' + struct.pack(">q", obj)

    elif isinstance(obj, float):
        return b'\xcb' + struct.pack(">d", obj)

    elif isinstance(obj, bytes):
        n = len(obj)
        if n <= 0xff:
            return b'\xc4' + bytes([n]) + obj
        elif n <= 0xffff:
            return b'\xc5' + struct.pack(">H", n) + obj
        else:
            return b'\xc6' + struct.pack(">I", n) + obj

    elif isinstance(obj, str):
        raw = obj.encode("utf-8")
        n = len(raw)
        if n <= 31:
            return bytes([0xa0 | n]) + raw
        elif n <= 0xff:
            return b'\xd9' + bytes([n]) + raw
        elif n <= 0xffff:
            return b'\xda' + struct.pack(">H", n) + raw
        else:
            return b'\xdb' + struct.pack(">I", n) + raw

    elif isinstance(obj, (list, tuple)):
        n = len(obj)
        if n <= 15:
            header = bytes([0x90 | n])
        elif n <= 0xffff:
            header = b'\xdc' + struct.pack(">H", n)
        else:
            header = b'\xdd' + struct.pack(">I", n)
        parts = [header]
        for item in obj:
            parts.append(packb(item))
        return b''.join(parts)

    elif isinstance(obj, dict):
        n = len(obj)
        if n <= 15:
            header = bytes([0x80 | n])
        elif n <= 0xffff:
            header = b'\xde' + struct.pack(">H", n)
        else:
            header = b'\xdf' + struct.pack(">I", n)
        parts = [header]
        for k, v in obj.items():
            parts.append(packb(k))
            parts.append(packb(v))
        return b''.join(parts)

    else:
        raise TypeError("Cannot pack type: " + str(type(obj)))


def unpackb(data):
    """Deserialize msgpack bytes to object"""
    obj, _ = _unpack(data, 0)
    return obj


def _unpack(data, offset):
    """Unpack one object starting at offset, return (obj, new_offset)"""
    b = data[offset]

    # Positive fixint (0x00 - 0x7f)
    if b <= 0x7f:
        return b, offset + 1

    # Fixmap (0x80 - 0x8f)
    elif 0x80 <= b <= 0x8f:
        n = b & 0x0f
        return _unpack_map(data, offset + 1, n)

    # Fixarray (0x90 - 0x9f)
    elif 0x90 <= b <= 0x9f:
        n = b & 0x0f
        return _unpack_array(data, offset + 1, n)

    # Fixstr (0xa0 - 0xbf)
    elif 0xa0 <= b <= 0xbf:
        n = b & 0x1f
        s = data[offset + 1:offset + 1 + n].decode("utf-8")
        return s, offset + 1 + n

    # Nil
    elif b == 0xc0:
        return None, offset + 1

    # False
    elif b == 0xc2:
        return False, offset + 1

    # True
    elif b == 0xc3:
        return True, offset + 1

    # Bin8
    elif b == 0xc4:
        n = data[offset + 1]
        return bytes(data[offset + 2:offset + 2 + n]), offset + 2 + n

    # Bin16
    elif b == 0xc5:
        n = struct.unpack_from(">H", data, offset + 1)[0]
        return bytes(data[offset + 3:offset + 3 + n]), offset + 3 + n

    # Bin32
    elif b == 0xc6:
        n = struct.unpack_from(">I", data, offset + 1)[0]
        return bytes(data[offset + 5:offset + 5 + n]), offset + 5 + n

    # Float32
    elif b == 0xca:
        return struct.unpack_from(">f", data, offset + 1)[0], offset + 5

    # Float64
    elif b == 0xcb:
        return struct.unpack_from(">d", data, offset + 1)[0], offset + 9

    # Uint8
    elif b == 0xcc:
        return data[offset + 1], offset + 2

    # Uint16
    elif b == 0xcd:
        return struct.unpack_from(">H", data, offset + 1)[0], offset + 3

    # Uint32
    elif b == 0xce:
        return struct.unpack_from(">I", data, offset + 1)[0], offset + 5

    # Uint64
    elif b == 0xcf:
        return struct.unpack_from(">Q", data, offset + 1)[0], offset + 9

    # Int8
    elif b == 0xd0:
        return struct.unpack_from("b", data, offset + 1)[0], offset + 2

    # Int16
    elif b == 0xd1:
        return struct.unpack_from(">h", data, offset + 1)[0], offset + 3

    # Int32
    elif b == 0xd2:
        return struct.unpack_from(">i", data, offset + 1)[0], offset + 5

    # Int64
    elif b == 0xd3:
        return struct.unpack_from(">q", data, offset + 1)[0], offset + 9

    # Str8
    elif b == 0xd9:
        n = data[offset + 1]
        s = data[offset + 2:offset + 2 + n].decode("utf-8")
        return s, offset + 2 + n

    # Str16
    elif b == 0xda:
        n = struct.unpack_from(">H", data, offset + 1)[0]
        s = data[offset + 3:offset + 3 + n].decode("utf-8")
        return s, offset + 3 + n

    # Str32
    elif b == 0xdb:
        n = struct.unpack_from(">I", data, offset + 1)[0]
        s = data[offset + 5:offset + 5 + n].decode("utf-8")
        return s, offset + 5 + n

    # Array16
    elif b == 0xdc:
        n = struct.unpack_from(">H", data, offset + 1)[0]
        return _unpack_array(data, offset + 3, n)

    # Array32
    elif b == 0xdd:
        n = struct.unpack_from(">I", data, offset + 1)[0]
        return _unpack_array(data, offset + 5, n)

    # Map16
    elif b == 0xde:
        n = struct.unpack_from(">H", data, offset + 1)[0]
        return _unpack_map(data, offset + 3, n)

    # Map32
    elif b == 0xdf:
        n = struct.unpack_from(">I", data, offset + 1)[0]
        return _unpack_map(data, offset + 5, n)

    # Negative fixint (0xe0 - 0xff)
    elif b >= 0xe0:
        return b - 256, offset + 1

    else:
        raise ValueError("Unknown msgpack byte: 0x{:02x}".format(b))


def _unpack_array(data, offset, n):
    result = []
    for _ in range(n):
        item, offset = _unpack(data, offset)
        result.append(item)
    return result, offset


def _unpack_map(data, offset, n):
    result = {}
    for _ in range(n):
        key, offset = _unpack(data, offset)
        val, offset = _unpack(data, offset)
        result[key] = val
    return result, offset
