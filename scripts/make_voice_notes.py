"""Generate the demo voice notes (Dutch speech via espeak-ng, encoded like WhatsApp: Ogg/Opus mono) and the
fixtures index used by the development transcriber. The texts are fictional demo stories.

Run from the application directory:  python scripts/make_voice_notes.py
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

import espeakng_loader

OUT = Path(__file__).resolve().parents[1] / "vertelschat" / "data" / "demo_audio"

STORIES = [
    ("01-huis-zolder.ogg", "Marijke: het huis in Assendorp", "nl+f3",
     "Nou, eh, we woonden in Zwolle, in Assendorp. Een smal rijtjeshuis, met beneden de voorkamer en de achterkamer, "
     "en daarachter een klein keukentje waar mijn moeder altijd stond. Boven op zolder sliep ik, samen met mijn broer "
     "Henk. Dat was eigenlijk gewoon één grote ruimte met een gordijn ertussen. In de winter was het er ijskoud, dan "
     "zaten er ijsbloemen op het raam. En mijn moeder had daar haar naaimachine staan, een Singer met zo'n trapper. Als "
     "zij aan het naaien was, zat ik op de trap te luisteren naar dat geratel. Ja. Dat geluid, dat hoor ik eigenlijk nog "
     "steeds."),
    ("02-huis-was.ogg", "Marijke: aanvulling over de was op zolder", "nl+f3",
     "Oh, en wat ik nog vergat te vertellen. Als het regende, hing mijn moeder de was op zolder te drogen. Dan moesten "
     "Henk en ik tussen de lakens door naar bed. Dat vonden we prachtig, dat was net een doolhof."),
    ("03-zondag.ogg", "Marijke: zondag bij oma", "nl+f3",
     "Zondag was echt een andere dag, hoor. Eerst naar de kerk, in je nette kleren, en je mocht niet rennen of buiten "
     "spelen, want dat hoorde niet. Na de kerk gingen we koffie drinken bij oma Jansen. Zij had altijd zo'n trommel met "
     "gevulde koeken, en als je braaf was, kreeg je er een halve. Een halve, ja. 's Middags ging mijn vader een stuk "
     "wandelen langs de IJssel en dan mochten wij mee. En 's avonds was er soep. Altijd soep op zondag."),
    ("04-ijsbaan.ogg", "Marijke: hoe ze papa leerde kennen", "nl+f3",
     "Dat was in de winter van 1973, op de ijsbaan hier in Zwolle. Ik kon best aardig schaatsen, maar hij helemaal "
     "niet. Hij viel pal voor mijn voeten, zo languit op het ijs. En toen hij opkeek zei hij: nou, nu moet je me wel "
     "even overeind helpen. Dat heb ik gedaan. En daarna heeft hij me naar huis gebracht, lopend, met zijn fiets aan de "
     "hand, want hij durfde niet te vragen of ik achterop wilde. Een jaar later waren we getrouwd."),
    ("05-bakker.ogg", "Marijke: het eerste baantje", "nl+f3",
     "Mijn eerste baantje was bij bakkerij Brink, op de hoek van onze straat. Ik was vijftien. Op zaterdag moest ik om "
     "zes uur beginnen, dan was het brood nog warm. Ik verdiende twee gulden vijftig per dag, en de helft moest ik thuis "
     "afgeven. Van de rest kocht ik een keer een paar nylonkousen, daar was ik zo trots op. Later, in 1970, ben ik naar "
     "Rotterdam gegaan voor de opleiding tot verpleegkundige. Dat was wel even wennen, zo'n grote stad."),
    ("06-hachee.ogg", "Marijke: het gerecht van moeder", "nl+f3",
     "Hachee. Mijn moeder maakte hachee op zaterdag. Dan stond die pan de hele middag op het fornuis te pruttelen, met "
     "laurierblaadjes en kruidnagel en een scheutje azijn. Het hele huis rook ernaar. En dan aten we het met rode kool "
     "en gekookte aardappelen. Ik heb het recept nooit opgeschreven, maar ik maak het nog precies zo. Dat kan ik jullie "
     "een keer laten zien."),
    ("07-ommen.ogg", "Marijke: kamperen in Ommen", "nl+f3",
     "Elk jaar gingen we kamperen in Ommen, aan de Vecht. We hadden zo'n bolle tent van oranje katoen, en als het "
     "regende moest je vooral niet tegen het doek aan komen, want dan lekte het. Mijn vader deed dan net alsof het "
     "allemaal heel gezellig was. Henk en ik zwommen de hele dag in de rivier. 's Avonds bakten we eieren op een klein "
     "gasstelletje. Ik denk dat dat de mooiste weken van het jaar waren."),
    ("08-elfstedentocht.ogg", "Marijke: verteld zonder vraag (winter 1963)", "nl+f3",
     "Ik zag vanochtend de eerste sneeuw, en toen moest ik ineens denken aan de winter van 1963, toen de "
     "Elfstedentocht werd gereden. Mijn vader zat de hele dag aan de radio. Hij zei: kijk, dit zie je maar één keer in "
     "je leven. Nou, dat bleek later niet helemaal waar te zijn."),
    ("09-doorgestuurd-henk.ogg", "Doorgestuurd: bericht van Henk", "nl",
     "Hoi Mar, met Henk. Ik bel je vanavond nog even terug over zondag. Doei!"),
]


def synth(text: str, voice: str) -> bytes:
    lib = ctypes.CDLL(espeakng_loader.get_library_path())
    samples = bytearray()
    CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)

    def cb(wav, n, events):
        if n > 0:
            samples.extend(ctypes.string_at(wav, n * 2))
        return 0

    callback = CB(cb)
    rate = lib.espeak_Initialize(2, 0, espeakng_loader.get_data_path().encode(), 0)
    lib.espeak_SetSynthCallback(callback)
    lib.espeak_SetVoiceByName(voice.encode())
    lib.espeak_SetParameter(1, 138, 0)  # rate (words per minute)
    lib.espeak_SetParameter(3, 42, 0)   # pitch
    lib.espeak_SetParameter(7, 7, 0)    # word gap (x10 ms): a calmer, older speaking pace
    data = text.encode("utf-8")
    lib.espeak_Synth(data, len(data) + 1, 0, 1, 0, 1, None, None)
    lib.espeak_Synchronize()
    lib.espeak_Terminate()
    assert rate > 0 and samples, "espeak produced no audio"
    return bytes(samples), rate


def encode(pcm: bytes, rate: int, dest: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "a.raw"
        raw.write_bytes(pcm)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", str(raw),
                        "-af", "adelay=700|700,apad=pad_dur=0.8,highpass=f=90,volume=1.4",
                        "-c:a", "libopus", "-b:a", "24k", "-ar", "16000", "-ac", "1", "-application", "voip",
                        "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact", str(dest)],
                       check=True)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fixtures = {}
    for name, label, voice, text in STORIES:
        pcm, rate = synth(text, voice)
        dest = OUT / name
        encode(pcm, rate, dest)
        digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        fixtures[digest] = {"file": name, "label": label, "text": text}
        print(f"{name}: {dest.stat().st_size // 1024} kB")
    (OUT / "fixtures.json").write_text(json.dumps(fixtures, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
