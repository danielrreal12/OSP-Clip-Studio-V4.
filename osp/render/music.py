"""An original instrumental pulse, synthesized locally without external recordings."""
from __future__ import annotations

import wave
import numpy as np


def make_instrumental(path, plan):
    rate, bpm = 24000, 96
    length = min(65, sum(s["output_duration"] for s in plan["segments"]) + 2)
    audio = np.zeros(int(length * rate), dtype=np.float32)
    rng = np.random.default_rng(96)

    def add(at, sound):
        offset = int(at * rate)
        if offset < 0 or offset >= len(audio):
            return
        n = min(len(sound), len(audio)-offset)
        audio[offset:offset+n] += sound[:n]

    beat = 60 / bpm
    for n in range(int(length / beat) + 1):
        at = n * beat
        t = np.arange(int(rate*.35)) / rate
        kick = .7 * np.sin(2*np.pi*(44*t + 7*(1-np.exp(-t*35)))) * np.exp(-t*15)
        if n % 4 in (0, 2):
            add(at, kick)
        if n % 4 in (1, 3):
            noise = rng.normal(0, 1, int(rate*.14))
            clap = .12 * np.diff(noise, prepend=0) * np.exp(-np.arange(len(noise))/rate*35)
            add(at, clap)
        for off, gain in [(0, .055), (.5, .035)]:
            noise = rng.normal(0, 1, int(rate*.06))
            add(at + off*beat, gain*np.diff(noise, prepend=0)*np.exp(-np.arange(len(noise))/rate*90))
        bass_note = [73.416, 73.416, 65.406, 87.307][(n//4) % 4]
        t = np.arange(int(rate*beat*.7)) / rate
        bass = (.13*np.sin(2*np.pi*bass_note*t) + .065*np.sin(4*np.pi*bass_note*t)) * np.minimum(1,t*70)*np.exp(-t*5)
        add(at + beat*.25, bass)
        if n % 4 == 0:
            t = np.arange(int(rate*beat*3.5)) / rate
            chord = sum(np.sin(2*np.pi*f*t) for f in [146.832, 174.614, 220]) / 3
            add(at, .055*chord*np.minimum(1,t*6)*np.exp(-t*1.5))

    elapsed = 0
    for i, shot in enumerate(plan["segments"]):
        if i and i <= 3:
            n = int(rate*.16)
            noise = rng.normal(0,1,n)
            envelope = np.sin(np.linspace(0,np.pi,n))**2
            add(max(0,elapsed-.08), .035*np.diff(noise,prepend=0)*envelope)
        elapsed += shot["output_duration"] - (shot["transition_in"]["duration"] if i else 0)
    fade = min(int(rate*.65), len(audio))
    audio[:int(rate*.04)] *= np.linspace(0,1,int(rate*.04))
    audio[-fade:] *= np.linspace(1,0,fade)
    audio = np.tanh(audio) * .85
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((audio*32767).astype("<i2").tobytes())
    return path
