import uuid
from typing import Any

from django.db import migrations, models


def fill_ids(apps: Any, schema_editor: Any) -> None:
    Check = apps.get_model("api", "Check")
    for check in (
        Check.objects.using(schema_editor.connection.alias).only("pk").iterator()
    ):
        check.dependency_id = uuid.uuid4()
        check.save(update_fields=("dependency_id",))


class Migration(migrations.Migration):
    dependencies = [("api", "0124_check_dependencies")]

    operations = [
        migrations.AddField(
            model_name="check",
            name="shared",
            field=models.BooleanField(default=False, db_index=True),
        ),
        migrations.AddField(
            model_name="check",
            name="dependency_id",
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.RunPython(fill_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="check",
            name="dependency_id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
