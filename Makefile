build:
	swiftc -O -o song_eater/audio_tap song_eater/audio_tap.swift \
		-framework ScreenCaptureKit -framework CoreMedia \
		-Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist \
		-Xlinker song_eater/Info.plist
	codesign --force --sign - --entitlements song_eater/entitlements.plist song_eater/audio_tap

install: build
	pip install -e .

clean:
	rm -f song_eater/audio_tap

.PHONY: build install clean
