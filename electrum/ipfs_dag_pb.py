# https://github.com/ipld/js-dag-pb/blob/master/src/pb-decode.js 

def decode_var_int(b, offset):
    v = 0
    shift = 0
    while True:
        if shift >= 64:
            raise Exception('varint overflow')
        if offset >= len(b):
            raise Exception('unexpected end of data')
        num = b[offset]
        offset += 1
        v += ((num & 0x7f) << shift) if (shift < 28) else ((num & 0x7f) * pow(2, shift))
        shift += 7
        if num < 0x80:
            break
    return (v, offset)
