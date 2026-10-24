# Vespa application for wardowl

`app/` is a single-node application package with the schema `VespaBackend` expects, for 16-dimensional
vectors (`wardowl.backends.vespa.schema(16)` writes `app/schemas/wardowl.sd`). CI deploys it the
same way:

```bash
docker run -d --name vespa -p 8080:8080 -p 19071:19071 vespaengine/vespa:8.763.13
(cd app && zip -qr - .) | curl -s --header Content-Type:application/zip --data-binary @- \
  http://localhost:19071/application/v2/tenant/default/prepareandactivate
```
