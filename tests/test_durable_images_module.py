"""pi durable's ``test/images-wasm-module.test.ts``.

Upstream checks that ``createPhotonImages`` takes a compiled WebAssembly module of Photon, as Cloudflare Workers pass
it, and initializes Photon from it: in its own file, so Photon is not yet initialized. misaka's ``misaka.durable.images``
uses Pillow instead of Photon (see its module docstring); ``createPhotonImages`` keeps upstream's ``wasm`` parameter and
ignores it. What the case means here: a processor created as the module documents, with that argument passed, works at
once, with nothing initialized before: the same 1x1 GIF is returned as it is once it decodes, as upstream asserts.
"""

from __future__ import annotations

import base64

from misaka.durable.images import createPhotonImages
from misaka.durable.tools import DEFAULT_IMAGE_LIMITS


async def test_create_photon_images_takes_a_compiled_web_assembly_module_as_cloudflare_workers_pass_it() -> None:
    # Upstream's compiled Photon module; misaka's processor needs none and ignores it.
    module = object()
    images = createPhotonImages(module)
    # A 1x1 GIF; returned as it is only once it decodes, which fails without a working decoder.
    data = "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    prepared = await images.prepare(base64.b64decode(data), "image/gif", DEFAULT_IMAGE_LIMITS)
    assert prepared == {"data": data, "mimeType": "image/gif"}
