import numpy as np
import pandas as pd
import torch, torchmetrics, wandb, transformers
import torch.nn as nn
import torch.nn.functional as F
import pytorch_lightning as pl, matplotlib.pyplot as plt
from collections import OrderedDict
from pytorch_lightning.loggers import WandbLogger


def SMAPE_loss(output, target):
    return torch.abs(output - target).sum() / (output + target).sum()


class DumpCore(nn.Module):
    def forward(self, x):
        return x


class BruceCNNCell(nn.Module):
    def __init__(self, **kwargs):
        super(BruceCNNCell, self).__init__()

        assert kwargs['num_cnn'] <= len(kwargs['kernel_size'])

        cnn_stack = OrderedDict()
        out_c = 524
        for i in range(kwargs['num_cnn']):
            cnn_layer = nn.Sequential(
                nn.Conv1d(1 if i == 0 else kwargs['output_channel'][i - 1],
                          kwargs['output_channel'][i],
                          kwargs['kernel_size'][i],
                          padding='same'),
                nn.LayerNorm(out_c),
                nn.MaxPool1d(2),
                nn.Dropout(0.1),
            )
            out_c = out_c // 2
            cnn_stack[f'cnn_{i}'] = cnn_layer
        self.cnn_layers = nn.Sequential(cnn_stack)

        self.flatten = nn.Flatten()
        self.cnn_out = nn.Linear(kwargs['output_channel'][i] * out_c, kwargs['core_out'])
        self.act = nn.GELU()
        self.dropout_cnn = nn.Dropout(0.1)

    def forward(self, inputs):
        b, f = inputs.shape
        x = inputs.reshape(b, 1, f).float()

        for layer in self.cnn_layers:
            x = layer(x)

        x = self.flatten(x)
        x = self.act(self.cnn_out(x))
        x = self.dropout_cnn(x)

        return x


class BruceStackedConv(nn.Module):
    def __init__(self, in_channel=1, out_channel=2, kernel_size=3, **kwargs):
        super().__init__()
        self.stacked_conv = nn.Sequential(
            nn.Conv1d(in_channel, out_channel, kernel_size=kernel_size, padding='same'),
            nn.BatchNorm1d(out_channel),
            nn.GELU(),
            nn.Conv1d(out_channel, out_channel, kernel_size=kernel_size, padding='same'),
            nn.BatchNorm1d(out_channel),
            nn.GELU(),
            nn.Dropout(0.1),
        )

    def forward(self, inputs):
        return self.stacked_conv(inputs)


class BruceDown(nn.Module):
    def __init__(self, in_channel=1, out_channel=2, kernel_size=3, **kwargs):
        super(BruceDown, self).__init__()
        self.maxpool_cnn = nn.Sequential(
            nn.MaxPool1d(2),
            BruceStackedConv(in_channel, out_channel, kernel_size, **kwargs)
        )

    def forward(self, inputs):
        return self.maxpool_cnn(inputs)


class BruceUp(nn.Module):
    def __init__(self, in_channel=1, out_channel=2, kernel_size=3, **kwargs):
        super(BruceUp, self).__init__()
        self.up_cnn = nn.Sequential(
            nn.Upsample(scale_factor=2),
            BruceStackedConv(in_channel, out_channel, kernel_size, **kwargs)
        )

    def forward(self, inputs, hidden_state):
        return self.up_cnn(inputs) + hidden_state


class BruceProcessingModule(nn.Module):
    def __init__(self, num_feature=512, out_channel=64, kernel_size=3, **kwargs):
        super(BruceProcessingModule, self).__init__()
        self.processing_module = nn.Sequential(
            nn.Linear(524, num_feature),
            nn.Tanh(),
            nn.Conv1d(1, out_channel, kernel_size=kernel_size, padding='same'),
            nn.BatchNorm1d(out_channel),
            nn.GELU(),
            nn.Conv1d(out_channel, out_channel, kernel_size=kernel_size, padding='same'),
            nn.BatchNorm1d(out_channel),
            nn.GELU(),
            nn.Dropout(0.1),
        )

    def forward(self, inputs):
        b, f = inputs.shape
        inputs = inputs.reshape(b, 1, f).float()
        return self.processing_module(inputs)


class BruceUNet(nn.Module):
    def __init__(self, num_feature=512, output_channel=[64, 128], kernel_size=[3, 3], core_out=512, **kwargs):
        super(BruceUNet, self).__init__()
        self.processing_input = BruceProcessingModule(num_feature=core_out,
                                                      out_channel=output_channel[0],
                                                      kernel_size=kernel_size[0])

        # Build Down Blocks
        downs = []
        for i in range(1, len(output_channel)):
            downs.append(BruceDown(
                in_channel=output_channel[i-1],
                out_channel=output_channel[i],
                kernel_size=kernel_size[i]
            ))
        self.down_blocks = nn.ModuleList(downs)

        # Build Up Blocks
        ups = []
        for i in range(len(output_channel)-1, 0, -1):
            ups.append(BruceUp(
                in_channel=output_channel[i],
                out_channel=output_channel[i-1],
                kernel_size=kernel_size[i]
            ))
        self.up_blocks = nn.ModuleList(ups)

        self.unet_out = nn.Sequential(
            nn.Conv1d(output_channel[0], output_channel[0], kernel_size=kernel_size[0], padding='same'),
            nn.BatchNorm1d(output_channel[0]),
            nn.GELU(),
            nn.Conv1d(in_channels=output_channel[0], out_channels=2, kernel_size=1, padding='same'),
            nn.Dropout(0.1),
            nn.Flatten(),
        )

    def forward(self, x: torch.FloatTensor):
        hidden_states = []

        x = self.processing_input(x)
        hidden_states.insert(0, x)

        # Down forward
        for layer in self.down_blocks:
            x = layer(x)
            hidden_states.insert(0, x)

        # Up forward
        hidden_states.pop(0)
        for i, layer in enumerate(self.up_blocks):
            x = layer(x, hidden_states[i])

        return self.unet_out(x)


class BruceLSTMMCell(nn.Module):
    def __init__(self, hidden_size=64, num_lstm_layer=1, bi_di=False, **kwargs):
        super().__init__()
        self.lstm = nn.LSTM(input_size=60,
                            hidden_size=hidden_size,
                            batch_first=True,
                            num_layers=num_lstm_layer,
                            dropout=0.1,
                            bidirectional=bi_di,
                            )
        self.norm = nn.LayerNorm(hidden_size if not bi_di else hidden_size * 2)

    def forward(self, inputs):
        x = self.lstm(inputs)[0][:, -1, :]
        x = self.norm(x)
        return x


class BruceLSTMBlock(nn.Module):
    def __init__(self, **kwargs):
        super(BruceLSTMBlock, self).__init__()
        self.pos_embedding = nn.Embedding(10, 60)
        self.pre_norm = nn.LayerNorm(60)
        self.lstm = BruceLSTMMCell(**kwargs)

    def forward(self, inputs):
        b, f = inputs.shape
        inputs = inputs.reshape(b, 10, 60)

        pos_matrix = self.pos_embedding(torch.arange(10, device=inputs.device).expand(b, 10))
        inputs = self.pre_norm(inputs + pos_matrix)

        return self.lstm(inputs)


class BruceModel(pl.LightningModule):
    def __init__(self, **kwargs):
        super().__init__()
        self.__dict__.update(kwargs)
        self.save_hyperparameters()
        # self.save_hyperparameters(kwargs)

        # Set core layer
        if kwargs['backbone'] == 'cnn':
            self.core = BruceCNNCell(**kwargs)
        elif kwargs['backbone'] == 'lstm':
            self.core = BruceLSTMBlock(**kwargs)
            self.core_out = self.hidden_size if not self.bi_di else self.hidden_size * 2
        elif kwargs['backbone'] == 'unet':
            self.core = BruceUNet(**kwargs)
            self.core_out *= 2
        else:
            self.core = nn.Sequential(nn.Linear(524, self.core_out), nn.GELU())

        # FCN Layer
        act_fn = nn.Tanh if self.act == 'tanh' else nn.GELU
        self.intermediate_layer = nn.Sequential(
            nn.Linear(self.core_out, self.core_out * 4),
            act_fn(),
            nn.Linear(self.core_out * 4, self.core_out),
            act_fn(),
            nn.Dropout(0.1),
        )

        # Ouputs layers
        self.output_layer = nn.Linear(self.core_out, 3)

        # # Cls output
        # self.cls_out = nn.Linear(self.core_out, 1)
        #
        # # Regression output
        # self.cls_embed = nn.Embedding(2, self.core_out, padding_idx=0)
        # #self.layernorm_rgs = nn.LayerNorm(self.core_out)
        # self.dt_out = nn.Linear(self.core_out, 1)
        # self.id_out = nn.Linear(self.core_out, 1)

        # Loss function
        self.cls_loss_fn = nn.BCEWithLogitsLoss(pos_weight=torch.tensor([self.pos_weight]))
        self.rgs_loss_fn = nn.L1Loss() if self.rgs_loss == 'mae' else SMAPE_loss

        # # Metrics to log
        # self.train_acc = torchmetrics.Accuracy()
        # self.train_auc = torchmetrics.AUROC(pos_label=1)
        # self.val_acc = torchmetrics.Accuracy()
        # self.val_auc = torchmetrics.AUROC(pos_label=1)

        # Init weights
        self.apply(self._init_weights)

    def _init_weights(self, module: nn.Module):
        if isinstance(module, nn.Linear) or isinstance(module, nn.Conv1d):
            module.weight.data.normal_(mean=0.0, std=self.initializer_range)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=self.initializer_range)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()
        elif isinstance(module, nn.LayerNorm) or isinstance(module, nn.BatchNorm1d):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        elif isinstance(module, nn.LSTM):
            for k, t in module.named_parameters():
                if 'weight' in k:
                    t.data.normal_(mean=0.0, std=self.initializer_range)
                elif 'bias' in k:
                    t.data.zero_()

    def forward(self, inputs):
        b, f = inputs.shape

        # Core forward
        x = self.core(inputs)

        # FCN
        x = self.intermediate_layer(x)

        # Classification output
        outputs = self.output_layer(x).unsqueeze(0)
        cls_out, dt_out, id_out = outputs.T

        # # Regression output
        # bi_cls = (torch.sigmoid(cls_out) > 0.5).squeeze().long()
        # cls_embed = self.cls_embed(bi_cls)
        # x = x + cls_embed
        # dt_out = self.dt_out(x)
        # id_out = self.id_out(x)

        return cls_out, torch.abs(dt_out), id_out

    def loss(self, cls_out, dt_out, id_out, cls_labels, dt_labels, id_labels):
        return self.cls_loss_fn(cls_out, cls_labels.float()), \
               self.rgs_loss_fn(dt_out, dt_labels), \
               self.rgs_loss_fn(id_out, id_labels)

    def training_step(self, batch, batch_idx):
        if self.trainer.global_step == 0:
            wandb.define_metric('train/rgs_loss', summary='min', goal='minimize')
            wandb.define_metric('train/cls_loss', summary='min', goal='minimize')
        inputs, cls_labels, dt_labels, id_labels = batch
        cls_out, dt_out, id_out = self(inputs)

        # Using threshold
        cls_out = torch.sigmoid(cls_out)
        dt_out *= (cls_out >= 0.5)

        cls_loss, rgs_loss, id_loss = self.loss(cls_out, dt_out, id_out, cls_labels, dt_labels, id_labels)

        # Log loss
        self.log('train/cls_loss', cls_loss.item(), prog_bar=False)
        self.log('train/rgs_loss', rgs_loss.item(), prog_bar=True)
        self.log('train/id_loss', id_loss.item(), prog_bar=False)

        # Log learning rate to progress bar
        cur_lr = self.trainer.optimizers[0].param_groups[0]['lr']
        self.log("lr", cur_lr, logger=False, prog_bar=True, on_step=True, on_epoch=False)

        # # Calculate train metrics
        # self.train_acc(torch.sigmoid(cls_out), cls_labels.long())
        # self.train_auc(torch.sigmoid(cls_out), cls_labels.long())

        # Log train metrics
        # self.log('train/acc', self.train_acc)
        # self.log('train/auc', self.train_auc)

        # loss = cls_loss * self.loss_weights[0] + rgs_loss * self.loss_weights[1]
        if self.cls_only:
            return cls_loss
        elif self.rgs_only:
            return rgs_loss + id_loss
        else:
            return cls_loss + rgs_loss + id_loss

    def validation_step(self, batch, batch_idx):
        # Track best rgs loss
        if self.trainer.global_step == 0:
            wandb.define_metric('val/rgs_loss', summary='min', goal='minimize')
            wandb.define_metric('val/cls_loss', summary='min', goal='minimize')
        inputs, cls_labels, dt_labels, id_labels = batch
        cls_out, dt_out, id_out = self(inputs)

        # Using threshold
        cls_out = torch.sigmoid(cls_out)
        dt_out *= (cls_out >= 0.5)

        cls_loss, rgs_loss, id_loss = self.loss(cls_out, dt_out, id_out, cls_labels, dt_labels, id_labels)
        if self.cls_only:
            loss = cls_loss
        elif self.rgs_only:
            loss = rgs_loss + id_loss
        else:
            loss = cls_loss + rgs_loss + id_loss

        # Log loss
        self.log('val/cls_loss', cls_loss.item(), prog_bar=False)
        self.log('val/rgs_loss', rgs_loss.item(), prog_bar=True)
        self.log('val/id_loss', id_loss.item(), prog_bar=False)

        '''
        # Calculate train metrics
        self.val_acc(torch.sigmoid(cls_out), cls_labels.long())
        self.val_auc(torch.sigmoid(cls_out), cls_labels.long())
        '''

        # Log train metrics
        self.log('val/loss', loss.item())
        # self.log('val/acc', self.val_acc, prog_bar=False)
        # self.log('val/auc', self.val_auc, prog_bar=False)

        # Processing outputs
        self.cls_outs.extend(cls_out.cpu().reshape(-1).tolist())
        self.true_values.extend(dt_labels.cpu().reshape(-1).tolist())
        self.predicted_values.extend(dt_out.cpu().reshape(-1).tolist())

    def on_validation_epoch_start(self) -> None:
        self.true_values = []
        self.predicted_values = []
        self.cls_outs = []

    def on_validation_epoch_end(self) -> None:
        self.df = pd.DataFrame({
            'y_true': torch.tensor(self.true_values).numpy(),
            'y_predict': torch.tensor(self.predicted_values).numpy(),
        })

        possible_true = self.df.y_true.unique()
        log_dict = {}

        for k in possible_true:
            hist_data = np.histogram(self.df[self.df.y_true == k].y_predict.values, range=(0.0, 0.4), bins=8)
            log_dict[f'hist/{str(k)}'] = wandb.Histogram(np_histogram=hist_data)

        log_dict['hist/cls'] = wandb.Histogram(
            np_histogram=np.histogram(np.array(self.cls_outs), bins=4, range=(0.0, 1.0))
        )

        log_dict['epoch'] = self.trainer.current_epoch

        self.logger.experiment.log(log_dict)

        # df.to_csv('temp_prediction.csv', index=False) # Save as csv

        # df.plot.hist(column=['y_predict'], by='y_true', figsize=(15, 30))
        # plt.savefig('temp_histogram.jpg')
        #
        # self.true_values = []
        # self.predicted_values = []

    def save_df(self, logger: WandbLogger, current_epoch=None):
        # Save Result as Table
        wandb.Table.MAX_ROWS = 1000000
        # artifact = wandb.Artifact(name=f'run-{logger.experiment.id}', type='prediction')
        # artifact.add(
        #     wandb.Table(dataframe=self.df),
        #     name='prediction_values'
        # )
        # logger.experiment.log_artifact(artifact, aliases=['best'])
        #
        # # Save histogram to Wandb
        # im = plt.imread('temp_histogram.jpg')
        # logger.experiment.log({"img": [wandb.Image(im)]}, commit=False)

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(self.parameters(), lr=self.lr)
        if self.scheduler:
            return {
                'optimizer': optimizer,
                'lr_scheduler': {
                    'scheduler': transformers.get_cosine_schedule_with_warmup(
                        optimizer,
                        num_warmup_steps=self.warming_step,
                        num_training_steps=self.total_training_step - self.warming_step
                    ),
                    'interval': 'step',
                }
            }
        else:
            return optimizer
