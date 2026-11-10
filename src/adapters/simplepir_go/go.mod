module simplepir_adapter

go 1.21

require github.com/ahenzinger/simplepir v0.0.0

replace github.com/ahenzinger/simplepir => ../../../third_party/simplepir

require github.com/imankalyan/cs4406-sem-project/src/adapters/serve_go v0.0.0

replace github.com/imankalyan/cs4406-sem-project/src/adapters/serve_go => ../serve_go
