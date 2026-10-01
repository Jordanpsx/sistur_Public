# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Modelo GeofenceLocation — zonas de localização autorizadas para registro de ponto.

Cada registro define um círculo geográfico (lat, lng + raio em metros) dentro
do qual o colaborador deve estar para registrar o ponto eletrônico.

A validação é feita via fórmula de Haversine em PontoService._esta_dentro_do_geofence().
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.extensions import db


class GeofenceLocation(db.Model):
    """Zona geográfica autorizada para registro de ponto eletrônico.

    Attributes:
        id: Chave primária autoincrement.
        name: Nome descritivo da zona (ex: "Sede Central", "Filial Norte").
        latitude: Latitude do centro da zona em graus decimais.
        longitude: Longitude do centro da zona em graus decimais.
        radius_meters: Raio da zona em metros. Colaborador deve estar dentro desse raio.
        is_active: Indica se a zona está ativa. Apenas zonas ativas são consideradas na validação.
        criado_em: Timestamp de criação (UTC).
        atualizado_em: Timestamp da última atualização (UTC).
    """

    __tablename__ = "sistur_geofence_locations"

    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(100), nullable=False)
    latitude      = db.Column(db.Float, nullable=False)
    longitude     = db.Column(db.Float, nullable=False)
    radius_meters = db.Column(db.Integer, nullable=False, default=200)
    is_active     = db.Column(db.Boolean, nullable=False, default=True)
    criado_em     = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    atualizado_em = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<GeofenceLocation id={self.id} name={self.name!r} raio={self.radius_meters}m>"
