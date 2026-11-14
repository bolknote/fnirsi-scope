#!/usr/bin/env python3
"""Safety checks for the physical r6j test's handling of uncertain commands."""
import contextlib
import importlib.util
import io
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC=importlib.util.spec_from_file_location('verify_r6j_device',Path(__file__).with_name('verify_r6j_device.py'))
probe=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(probe)

class FakeScope:
    def __init__(self, failure):self.failure=failure;self.commands=[];self.pending=False;self.running=False;self.id=0
    def settings(self):
        return {'calibration_active':False,'spi_fault':False,'math_mode':False,
                'channels':[{'fft':False},{'fft':False}],'running':self.running}
    def transfer(self,request):
        data=bytearray(64)
        if request==0x5c:data[63]=4;data[57]=self.running
        if request==0x60:struct.pack_into('<II',data,0,0x364c4143,1)
        return bytes(data)
    def status(self):return {'command_id':1 if self.pending else 0,'command_done':0,
                              'frame_id':self.id,'phase':'ready','frame_crc32':0}
    def command(self,op,arg=0,timeout=0):
        self.commands.append(op)
        if op==25:
            if self.failure=='start-timeout':raise probe.scope_control.ScopeError('timeout')
            self.running=True
        if op==34:
            if self.failure=='live-pending':
                self.pending=True;raise probe.scope_control.ScopeError('timeout')
            self.id+=1;return self.status()
        if op==26:self.running=False
        return self.status()
    def frame(self,status):raise RuntimeError('CRC mismatch')
    def close(self):pass

class Tests(unittest.TestCase):
    def run_probe(self,mode):
        fake=FakeScope(mode)
        with tempfile.TemporaryDirectory() as root, \
             patch.object(probe,'ROOT',Path(root)), \
             patch.object(probe.scope_control,'Scope',return_value=fake), \
             patch.object(sys,'argv',['verify_r6j_device.py','--seconds','1']), \
             contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(probe.main(),1)
        return fake.commands

    def test_start_timeout_does_not_send_a_blind_stop(self):
        self.assertEqual(self.run_probe('start-timeout'),[25])
    def test_pending_live_command_does_not_send_a_blind_stop(self):
        self.assertEqual(self.run_probe('live-pending'),[25,34])
    def test_failed_frame_with_idle_mailbox_stops_once(self):
        self.assertEqual(self.run_probe('frame-error'),[25,34,26])

if __name__=='__main__':unittest.main()
