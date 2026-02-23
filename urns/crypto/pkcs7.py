# µReticulum PKCS7 padding

class PKCS7:
    BLOCKSIZE = 16

    @staticmethod
    def pad(data, bs=BLOCKSIZE):
        n = bs - len(data) % bs
        return data + bytes([n]) * n

    @staticmethod
    def unpad(data, bs=BLOCKSIZE):
        n = data[-1]
        if n > bs:
            raise ValueError("Invalid padding length: " + str(n))
        return data[:len(data) - n]
