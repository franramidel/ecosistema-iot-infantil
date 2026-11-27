from machine import Pin, bitstream
import time

class HX711:
    def __init__(self, d_out, pd_sck, gain=128):
        self.pSCK = pd_sck
        self.pOUT = d_out
        self.pSCK.value(False)
        self.GAIN = 0
        self.OFFSET = 0
        self.SCALE = 1
        self.set_gain(gain)

    def set_gain(self, gain):
        if gain == 128:
            self.GAIN = 1
        elif gain == 64:
            self.GAIN = 3
        elif gain == 32:
            self.GAIN = 2
        self.read()
        return True

    def is_ready(self):
        return self.pOUT.value() == 0

    def read(self):
        # Esperar a que el chip baje la señal a 0
        while self.pOUT.value() == 1:
            pass
        
        data = 0
        # Leer los 24 bits manualmente
        for _ in range(24):
            self.pSCK.value(True)
            data = (data << 1) | self.pOUT.value()
            self.pSCK.value(False)
            
        # Enviar pulso para configurar la ganancia
        for _ in range(self.GAIN):
            self.pSCK.value(True)
            self.pSCK.value(False)
            
        # Convertir a complemento a dos (números negativos)
        if data & 0x800000:
            data -= 0x1000000
            
        return data

    def tare(self, times=10):
        self.OFFSET = 0
        total = 0
        for _ in range(times):
            total += self.read()
            time.sleep_ms(10)
        self.OFFSET = total / times
        return self.OFFSET

    def get_value(self, times=1):
        total = 0
        for _ in range(times):
            total += self.read()
            time.sleep_ms(5)
        return (total / times) - self.OFFSET