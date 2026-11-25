"""A foto pública do Mercos entra no produto sem apagar a foto do operador."""

from app.erp.services.product_images import apply_media, media_from_payload


def test_storefront_payload_keeps_https_images_and_parent():
    media = media_from_payload({
        "imagem": "https://arquivos.mercos.com/media/imagem_produto/1/a.png",
        "imagens": [
            "https://arquivos.mercos.com/media/imagem_produto/1/a.png",
            "https://arquivos.mercos.com/media/imagem_produto/1/b.png",
            "abc-hash",
        ],
        "videos": ["https://arquivos.mercos.com/media/video/1.mp4"],
        "produto_agregador_id": 99,
        "saldo_estoque": None,
    })
    assert media["images"] == [
        "https://arquivos.mercos.com/media/imagem_produto/1/a.png",
        "https://arquivos.mercos.com/media/imagem_produto/1/b.png",
    ]
    assert media["videos"] == ["https://arquivos.mercos.com/media/video/1.mp4"]
    assert media["parentId"] == "99"


def test_operator_photo_is_kept():
    class Row:
        def __init__(self) -> None:
            self.image_url = "https://cdn.example/minha.jpg"
            self.photos = ["https://cdn.example/minha.jpg"]
            self.videos = None

    row = Row()
    assert apply_media(row, {"images": ["https://arquivos.mercos.com/a.png"], "videos": []}) is False
    assert row.image_url == "https://cdn.example/minha.jpg"
