import numpy as np
import pandas as pd, uuid, json, datetime, time, argparse, os, platform
from train import get_data
from ctypes import *

if platform.system() == 'Windows':
    CDLL("C:\\Users\\BruceNguyen\\miniconda3\\Lib\\site-packages\\confluent_kafka.libs\\librdkafka-5d2e2910.dll")

from confluent_kafka import SerializingProducer
from confluent_kafka.serialization import StringSerializer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroSerializer


class PigSensor(object):
    def __init__(self, inputs, target, time):
        self.inputs = inputs
        self.target = target
        self.time = time


def data_to_dict(obj: PigSensor, ctx):
    return obj.__dict__


def delivery_report(err, msg):
    if err is not None:
        print("Delivery failed for User record {}: {}".format(msg.key(), err))
        return
    print('User record {} successfully produced to {} [{}] at offset {}'.format(
        msg.key(), msg.topic(), msg.partition(), msg.offset()))


def process_data(path, MEAN=None, STD=None):
    inputs = []
    for folder in os.listdir(path):
        files = os.listdir(path + folder + '/ect_1/data1/2022/2022-11/2022-11-17')
        for f in files:
            df = pd.read_csv(path + folder + '/ect_1/data1/2022/2022-11/2022-11-17/' + f, sep='\t', index_col=0, header=None)
            inputs.append(df.iloc[:, :600].values)

    inputs = np.vstack(inputs)
    inputs[inputs > 1] = 0

    if MEAN is None:
        MEAN = inputs.mean()
        STD = inputs.std()

    inputs = (inputs - MEAN)/STD
    labels = np.array([0] * inputs.shape[0])

    return -inputs, labels


def producing(args):
    MEAN = -0.5485341293039697
    STD = 0.901363162490852
    train_inputs, train_cls_label, train_deposit_thickness, train_inner_diameter, _, _ = get_data(args.path,
                                                                                                  True,
                                                                                                  True,
                                                                                                  MEAN,
                                                                                                  STD)

    # train_inputs, train_deposit_thickness = process_data(args.path)

    df = pd.DataFrame({'inputs': train_inputs.tolist(), 'labels': train_deposit_thickness})
    # pd.DataFrame(np.hstack([train_inputs, train_deposit_thickness.reshape(-1,1)])).to_csv(f'./data/pig_2.csv', sep='\t', index=False)

    topic = args.topic

    schema_str = """
        {
            "namespace": "confluent.io.examples.serialization.avro",
            "name": "PigSensor",
            "type": "record",
            "fields": [
                {
                    "name": "inputs",
                    "type": {
                        "type": "array",
                        "items": "float",
                        "name": "input"
                    },
                    "default": []
                },
                {
                    "name": "target", 
                    "type": "float"
                },
                {
                    "name": "time",
                    "type": {
                        "type": "long",
                        "logicalType": "timestamp-millis"
                    }
                }
            ]
        }
        """

    schema_registry_conf = {'url': args.schema_registry}
    schema_registry_client = SchemaRegistryClient(schema_registry_conf)

    avro_serializer = AvroSerializer(schema_registry_client,
                                     schema_str,
                                     data_to_dict)

    producer_conf = {'bootstrap.servers': args.bootstrap_servers,
                     'key.serializer': StringSerializer('utf_8'),
                     'value.serializer': avro_serializer}

    producer = SerializingProducer(producer_conf)
    for i in range(df.shape[0]):
        producer.poll(0.0)
        try:
            mess_id = str(uuid.uuid4())
            date_time = datetime.datetime.now()
            data = PigSensor(inputs=df.inputs[i], target=df.labels[i], time=date_time)
            producer.produce(topic=topic, key=mess_id, value=data,
                             # on_delivery=delivery_report
                             )
        except KeyboardInterrupt:
            producer.flush()
            break
        except ValueError:
            print("Invalid input, discarding record...")
            continue

        print(' -- PRODUCER: Sent message at {}, message id {}'.format(
            date_time.strftime('%Y-%m-%d %H:%M:%S'),
            mess_id)
        )
        time.sleep(0.1)

    producer.flush()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-b', dest='bootstrap_servers', default='localhost:9092', type=str, help='Kafka Host')
    parser.add_argument('-s', dest="schema_registry", default='http://127.0.0.1:8081', help="Schema Registry")
    parser.add_argument('-t', dest="topic", default='pig-push-data', help="Topic name")
    parser.add_argument('-f', dest="path", default='val_new.h5', help="Topic name")
    # parser.add_argument('-f', dest="path",
    #                     default=r'C:\Users\BruceNguyen\Documents\Github\rocsole_dili\data\pipe_2mm_oil\\',
    #                     help="Topic name")
    args = parser.parse_args()

    producing(args)
