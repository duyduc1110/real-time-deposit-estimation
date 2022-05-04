import dash, json
import plotly.express as px, pandas as pd, numpy as np
import plotly.graph_objects as go
from dash import dcc, html
from dash.dependencies import Input, Output
from kafka import KafkaConsumer

app = dash.Dash(__name__)
app.layout = html.Div(
    html.Div([
        html.H1('PIG Demonstration'),
        dcc.Graph(id='live-update-graph'),
        dcc.Interval(
            id='interval-component',
            interval=1*1000,
            n_intervals=0
        )
    ])
)


@app.callback(Output('live-update-graph', 'figure'),
              Input('interval-component', 'n_intervals'))
def function_square(n):
    df_plot = pd.read_csv('df_to_plot.csv', index_col=False)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df_plot.idx, y=df_plot.target, name="target", line_shape='spline'))
    fig.add_trace(go.Scatter(x=df_plot.idx, y=df_plot.predict, name="predict", line_shape='spline'))
    return fig


if __name__ == '__main__':
    app.run_server(debug=False)