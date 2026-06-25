from django.conf import settings
from django.db import models
from django.db.models import BooleanField, Case, Exists, OuterRef, Value, When
from django.utils.translation import gettext_lazy as _

from core.models import TimestampMixin

__all__ = ("CarType", "CarQuerySet", "CarManager", "Car")


class CarType(models.Model):
    name = models.CharField(max_length=255, verbose_name=_("Name"))
    picture_url = models.URLField(blank=True, verbose_name=_("Picture URL"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Car type")
        verbose_name_plural = _("Car types")

    def __str__(self):
        return self.name


class CarQuerySet(models.QuerySet):
    def with_availability(self):
        """Annotate ``is_available``: status='active' AND no in-progress order."""
        # Lazy import: CarOrder lives in a sibling submodule; importing it at
        # module load would create a models-package cycle (orders.py → cars.py).
        from car_orders.models import CarOrder

        return self.annotate(
            is_available=Case(
                When(
                    status=Car.Status.ACTIVE,
                    then=~Exists(
                        CarOrder.objects.filter(
                            car_id=OuterRef("pk"),
                            status=CarOrder.Status.IN_PROGRESS,
                        )
                    ),
                ),
                default=Value(False),
                output_field=BooleanField(),
            ),
        )


class CarManager(models.Manager.from_queryset(CarQuerySet)):
    def get_queryset(self):
        return super().get_queryset().with_availability()


class Car(TimestampMixin):
    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        IN_REPAIR = "in_repair", _("In repair")
        DECOMMISSIONED = "decommissioned", _("Decommissioned")

    model = models.CharField(max_length=255, verbose_name=_("Model"))
    plate_number = models.CharField(
        max_length=50,
        unique=True,
        verbose_name=_("Plate number"),
    )
    type = models.ForeignKey(
        CarType,
        on_delete=models.PROTECT,
        related_name="cars",
        verbose_name=_("Type"),
    )
    num_seats = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_("Number of seats"),
    )
    picture_url = models.URLField(blank=True, verbose_name=_("Picture URL"))
    drivers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="driven_cars",
        verbose_name=_("Drivers"),
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
        verbose_name=_("Status"),
    )

    objects = CarManager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Car")
        verbose_name_plural = _("Cars")

    def __str__(self):
        return f"{self.model} ({self.plate_number})"
